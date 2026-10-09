package main

import (
	"encoding/binary"
	"encoding/csv"
	"flag"
	"fmt"
	"math"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"sync"
	"syscall"
	"time"
)

const (
	magic     uint32 = 0x46525454 // "FRTT"
	version   uint16 = 1
	headerLen        = 32
)

type sample struct {
	seq       uint64
	scheduled time.Time
	sent      time.Time
	received  time.Time
	rtt       time.Duration
	hasRecv   bool
	duplicate bool
}

type config struct {
	runID        string
	target       string
	port         int
	durationS    float64
	intervalMS   int
	payloadBytes int
	timeoutMS    int
	outDir       string
}

type receiverStats struct {
	truncated int
	overflow  uint32
}

func parseArgs() config {
	cfg := config{}
	flag.StringVar(&cfg.runID, "run-id", "", "run identifier written into CSV rows")
	flag.StringVar(&cfg.target, "target", "", "UDP echo server host or IP")
	flag.IntVar(&cfg.port, "port", 5001, "UDP echo server port")
	flag.Float64Var(&cfg.durationS, "duration-s", 1200, "probe duration in seconds")
	flag.IntVar(&cfg.intervalMS, "interval-ms", 10, "send interval in milliseconds")
	flag.IntVar(&cfg.payloadBytes, "payload-bytes", 64, "UDP payload size in bytes")
	flag.IntVar(&cfg.timeoutMS, "timeout-ms", 2000, "RTT timeout in milliseconds")
	flag.StringVar(&cfg.outDir, "out-dir", ".", "directory for fine_rtt_samples.csv")
	flag.Parse()

	if cfg.runID == "" {
		cfg.runID = fmt.Sprintf("fine_rtt_udp_%d", time.Now().Unix())
	}
	if cfg.target == "" {
		fail("missing --target")
	}
	if cfg.port <= 0 || cfg.port > 65535 {
		fail("invalid --port: %d", cfg.port)
	}
	if cfg.durationS <= 0 {
		fail("invalid --duration-s: %.3f", cfg.durationS)
	}
	if cfg.intervalMS <= 0 {
		fail("invalid --interval-ms: %d", cfg.intervalMS)
	}
	if cfg.timeoutMS <= 0 {
		fail("invalid --timeout-ms: %d", cfg.timeoutMS)
	}
	if cfg.payloadBytes < headerLen {
		fail("--payload-bytes must be at least %d", headerLen)
	}
	return cfg
}

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "ERROR: "+format+"\n", args...)
	os.Exit(2)
}

func makePayload(size int, seq uint64, scheduledRelNS int64, sendRelNS int64) []byte {
	payload := make([]byte, size)
	binary.BigEndian.PutUint32(payload[0:4], magic)
	binary.BigEndian.PutUint16(payload[4:6], version)
	binary.BigEndian.PutUint16(payload[6:8], headerLen)
	binary.BigEndian.PutUint64(payload[8:16], seq)
	binary.BigEndian.PutUint64(payload[16:24], uint64(scheduledRelNS))
	binary.BigEndian.PutUint64(payload[24:32], uint64(sendRelNS))
	return payload
}

func parseSeq(payload []byte) (uint64, bool) {
	if len(payload) < headerLen {
		return 0, false
	}
	if binary.BigEndian.Uint32(payload[0:4]) != magic {
		return 0, false
	}
	if binary.BigEndian.Uint16(payload[4:6]) != version {
		return 0, false
	}
	if int(binary.BigEndian.Uint16(payload[6:8])) > len(payload) {
		return 0, false
	}
	return binary.BigEndian.Uint64(payload[8:16]), true
}

func runSender(
	conn *net.UDPConn,
	cfg config,
	start time.Time,
	total int,
	samples map[uint64]*sample,
	mu *sync.Mutex,
	stop <-chan struct{},
) {
	interval := time.Duration(cfg.intervalMS) * time.Millisecond
	for i := 0; i < total; i++ {
		seq := uint64(i + 1)
		scheduled := start.Add(time.Duration(i) * interval)
		if delay := time.Until(scheduled); delay > 0 {
			timer := time.NewTimer(delay)
			select {
			case <-stop:
				timer.Stop()
				return
			case <-timer.C:
			}
		} else {
			select {
			case <-stop:
				return
			default:
			}
		}

		sent := time.Now()
		row := &sample{seq: seq, scheduled: scheduled, sent: sent}
		mu.Lock()
		samples[seq] = row
		mu.Unlock()

		payload := makePayload(
			cfg.payloadBytes,
			seq,
			scheduled.Sub(start).Nanoseconds(),
			sent.Sub(start).Nanoseconds(),
		)
		if _, err := conn.Write(payload); err != nil {
			fmt.Fprintf(os.Stderr, "WARNING: send seq=%d failed: %v\n", seq, err)
		}
	}
}

func runReceiver(conn *net.UDPConn, samples map[uint64]*sample, mu *sync.Mutex, stop <-chan struct{}, stats *receiverStats) {
	// The echo is a fixed 32-byte header.  Keep the receive buffer small so a
	// full payload is never copied into userspace; MSG_TRUNC still exposes the
	// wire length for diagnostics.
	buf := make([]byte, headerLen)
	oob := make([]byte, 128)
	for {
		select {
		case <-stop:
			return
		default:
		}

		_ = conn.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
		n, flags, oobn, _, err := conn.ReadMsgUDP(buf, oob)
		received := time.Now()
		if err != nil {
			if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
				continue
			}
			fmt.Fprintf(os.Stderr, "WARNING: receive failed: %v\n", err)
			continue
		}
		if flags&syscall.MSG_TRUNC != 0 {
			mu.Lock()
			stats.truncated++
			mu.Unlock()
		}
		if drops, ok := socketOverflow(oob[:oobn]); ok {
			mu.Lock()
			if drops > stats.overflow {
				stats.overflow = drops
			}
			mu.Unlock()
		}

		seq, ok := parseSeq(buf[:n])
		if !ok {
			continue
		}

		mu.Lock()
		row := samples[seq]
		if row != nil {
			if row.hasRecv {
				row.duplicate = true
			} else {
				row.received = received
				row.rtt = received.Sub(row.sent)
				row.hasRecv = true
			}
		}
		mu.Unlock()
	}
}

func fmtSeconds(d time.Duration) string {
	return fmt.Sprintf("%.9f", d.Seconds())
}

func fmtMillis(d time.Duration) string {
	return fmt.Sprintf("%.6f", float64(d.Nanoseconds())/1e6)
}

func socketOverflow(oob []byte) (uint32, bool) {
	messages, err := syscall.ParseSocketControlMessage(oob)
	if err != nil {
		return 0, false
	}
	for _, message := range messages {
		if message.Header.Level == syscall.SOL_SOCKET && message.Header.Type == 40 && len(message.Data) >= 4 {
			return binary.NativeEndian.Uint32(message.Data[:4]), true
		}
	}
	return 0, false
}

func writeSamples(cfg config, start time.Time, timeout time.Duration, samples map[uint64]*sample, stats receiverStats) error {
	if err := os.MkdirAll(cfg.outDir, 0755); err != nil {
		return err
	}

	csvPath := filepath.Join(cfg.outDir, "fine_rtt_samples.csv")
	file, err := os.Create(csvPath)
	if err != nil {
		return err
	}
	defer file.Close()

	writer := csv.NewWriter(file)
	defer writer.Flush()

	header := []string{
		"run_id",
		"target",
		"port",
		"interval_ms",
		"timeout_ms",
		"payload_bytes",
		"seq",
		"scheduled_send_rel_s",
		"send_rel_s",
		"send_lag_ms",
		"recv_rel_s",
		"rtt_ms",
		"lost",
		"late",
		"duplicate",
	}
	if err := writer.Write(header); err != nil {
		return err
	}

	seqs := make([]int, 0, len(samples))
	for seq := range samples {
		seqs = append(seqs, int(seq))
	}
	sort.Ints(seqs)

	for _, seqInt := range seqs {
		row := samples[uint64(seqInt)]
		late := row.hasRecv && row.rtt > timeout
		lost := !row.hasRecv || late

		recvRel := ""
		rttMS := ""
		if row.hasRecv {
			recvRel = fmtSeconds(row.received.Sub(start))
			rttMS = fmtMillis(row.rtt)
		}

		record := []string{
			cfg.runID,
			cfg.target,
			strconv.Itoa(cfg.port),
			strconv.Itoa(cfg.intervalMS),
			strconv.Itoa(cfg.timeoutMS),
			strconv.Itoa(cfg.payloadBytes),
			strconv.FormatUint(row.seq, 10),
			fmtSeconds(row.scheduled.Sub(start)),
			fmtSeconds(row.sent.Sub(start)),
			fmtMillis(row.sent.Sub(row.scheduled)),
			recvRel,
			rttMS,
			strconv.FormatBool(lost),
			strconv.FormatBool(late),
			strconv.FormatBool(row.duplicate),
		}
		if err := writer.Write(record); err != nil {
			return err
		}
	}

	if err := writer.Error(); err != nil {
		return err
	}

	return writeProbeSummary(cfg, timeout, samples, stats)
}

func writeProbeSummary(cfg config, timeout time.Duration, samples map[uint64]*sample, stats receiverStats) error {
	sent := len(samples)
	echoed := 0
	received := 0
	lost := 0
	late := 0
	duplicates := 0

	for _, row := range samples {
		isLate := row.hasRecv && row.rtt > timeout
		isLost := !row.hasRecv || isLate
		if row.hasRecv {
			echoed++
		}
		if !isLost {
			received++
		}
		if isLost {
			lost++
		}
		if isLate {
			late++
		}
		if row.duplicate {
			duplicates++
		}
	}

	lossRate := 0.0
	if sent > 0 {
		lossRate = float64(lost) / float64(sent)
	}

	lines := []string{
		fmt.Sprintf("run_id=%s", cfg.runID),
		fmt.Sprintf("target=%s", cfg.target),
		fmt.Sprintf("port=%d", cfg.port),
		fmt.Sprintf("duration_s=%.6f", cfg.durationS),
		fmt.Sprintf("interval_ms=%d", cfg.intervalMS),
		fmt.Sprintf("timeout_ms=%d", cfg.timeoutMS),
		fmt.Sprintf("payload_bytes=%d", cfg.payloadBytes),
		fmt.Sprintf("sent_packets=%d", sent),
		fmt.Sprintf("received_packets=%d", received),
		fmt.Sprintf("echoed_packets=%d", echoed),
		fmt.Sprintf("lost_packets=%d", lost),
		fmt.Sprintf("late_packets=%d", late),
		fmt.Sprintf("duplicate_packets=%d", duplicates),
		fmt.Sprintf("loss_rate=%.9f", lossRate),
		fmt.Sprintf("socket_rxq_overflow_packets=%d", stats.overflow),
		fmt.Sprintf("truncated_packets=%d", stats.truncated),
		"receive_api=readmsg",
		"header_only=true",
	}

	summaryPath := filepath.Join(cfg.outDir, "fine_rtt_probe_summary.txt")
	return os.WriteFile(summaryPath, []byte(joinLines(lines)), 0644)
}

func joinLines(lines []string) string {
	return fmt.Sprintln(linesToText(lines))
}

func linesToText(lines []string) string {
	if len(lines) == 0 {
		return ""
	}
	out := lines[0]
	for _, line := range lines[1:] {
		out += "\n" + line
	}
	return out
}

func main() {
	cfg := parseArgs()
	timeout := time.Duration(cfg.timeoutMS) * time.Millisecond
	total := int(math.Ceil(cfg.durationS * 1000.0 / float64(cfg.intervalMS)))
	if total <= 0 {
		fail("computed zero packets")
	}

	addr, err := net.ResolveUDPAddr("udp", fmt.Sprintf("%s:%d", cfg.target, cfg.port))
	if err != nil {
		fail("resolve UDP address failed: %v", err)
	}

	conn, err := net.DialUDP("udp", nil, addr)
	if err != nil {
		fail("dial UDP failed: %v", err)
	}
	defer conn.Close()
	if raw, rawErr := conn.SyscallConn(); rawErr == nil {
		_ = raw.Control(func(fd uintptr) {
			_ = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF, 64*1024*1024)
			_ = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, 40, 1) // SO_RXQ_OVFL
		})
	}

	stopSender := make(chan struct{})
	stopReceiver := make(chan struct{})
	var stopSenderOnce sync.Once
	interrupted := make(chan struct{})
	var interruptedOnce sync.Once
	signalStop := make(chan os.Signal, 1)
	signal.Notify(signalStop, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-signalStop
		fmt.Fprintln(os.Stderr, "WARNING: interrupted; writing partial results")
		stopSenderOnce.Do(func() { close(stopSender) })
		interruptedOnce.Do(func() { close(interrupted) })
	}()

	samples := make(map[uint64]*sample, total)
	var mu sync.Mutex
	var stats receiverStats
	var receiverWG sync.WaitGroup

	start := time.Now().Add(200 * time.Millisecond)
	receiverWG.Add(1)
	go func() {
		defer receiverWG.Done()
		runReceiver(conn, samples, &mu, stopReceiver, &stats)
	}()

	runSender(conn, cfg, start, total, samples, &mu, stopSender)

	select {
	case <-interrupted:
	case <-time.After(timeout + 200*time.Millisecond):
	}
	close(stopReceiver)
	receiverWG.Wait()

	mu.Lock()
	snapshot := make(map[uint64]*sample, len(samples))
	for seq, row := range samples {
		copied := *row
		snapshot[seq] = &copied
	}
	mu.Unlock()

	mu.Lock()
	statsSnapshot := stats
	mu.Unlock()
	if err := writeSamples(cfg, start, timeout, snapshot, statsSnapshot); err != nil {
		fail("write results failed: %v", err)
	}

	fmt.Printf("Done. Wrote %d samples to %s\n", len(snapshot), filepath.Join(cfg.outDir, "fine_rtt_samples.csv"))
}
