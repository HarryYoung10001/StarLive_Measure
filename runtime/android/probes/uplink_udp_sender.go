package main

import (
	"bufio"
	"compress/gzip"
	"encoding/binary"
	"encoding/csv"
	"flag"
	"fmt"
	"hash/fnv"
	"math"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	dataMagic       uint32 = 0x55574C50 // "UWLP"
	version         uint16 = 2
	headerLen              = 48
	legacyVersion   uint16 = 1
	legacyHeaderLen        = 40
	appName                = "uplink_udp_sender 2"
)

type config struct {
	runID            string
	target           string
	port             int
	durationS        float64
	rate             string
	payloadBytes     int
	outDir           string
	noCSV            bool
	progressInterval time.Duration
	durationMode     string
	showVersion      bool
}

type rateSpec struct {
	pps       float64
	interval  time.Duration
	totalPkts int
}

func parseArgs() config {
	cfg := config{}
	flag.StringVar(&cfg.runID, "run-id", "", "run identifier written into CSV rows")
	flag.StringVar(&cfg.target, "target", "", "server host/IP")
	flag.IntVar(&cfg.port, "port", 5004, "server UDP port")
	flag.Float64Var(&cfg.durationS, "duration-s", 150, "probe duration in seconds")
	flag.StringVar(&cfg.rate, "rate", "100pps", "send rate, e.g. 100pps, 100K, 1M")
	flag.IntVar(&cfg.payloadBytes, "payload-bytes", 1200, "UDP payload bytes")
	flag.StringVar(&cfg.outDir, "out-dir", ".", "directory for uplink_udp_sender.csv.gz")
	flag.BoolVar(&cfg.noCSV, "no-csv", false, "skip per-packet CSV logging and write only the summary")
	flag.DurationVar(&cfg.progressInterval, "progress-interval", time.Second, "progress heartbeat interval")
	flag.StringVar(&cfg.durationMode, "duration-mode", "packet-count", "duration mode: packet-count or wall-clock")
	flag.BoolVar(&cfg.showVersion, "version", false, "print version and exit")
	flag.Parse()

	if cfg.showVersion {
		return cfg
	}
	if cfg.runID == "" {
		cfg.runID = fmt.Sprintf("uplink_udp_loss_%d", time.Now().Unix())
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
	if cfg.durationMode != "packet-count" && cfg.durationMode != "wall-clock" {
		fail("invalid --duration-mode: %s", cfg.durationMode)
	}
	if cfg.progressInterval <= 0 {
		fail("invalid --progress-interval: %s", cfg.progressInterval)
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

func parseRate(rate string, payloadBytes int, durationS float64) rateSpec {
	var number float64
	var suffix string
	normalized := strings.ToLower(strings.TrimSpace(rate))

	for _, candidate := range []string{"gbps", "mbps", "kbps", "pps", "g", "m", "k"} {
		if len(normalized) > len(candidate) && normalized[len(normalized)-len(candidate):] == candidate {
			suffix = candidate
			break
		}
	}

	if suffix == "" {
		if _, err := fmt.Sscanf(normalized, "%f", &number); err != nil {
			fail("invalid --rate: %s", rate)
		}
		suffix = "pps"
	} else {
		if _, err := fmt.Sscanf(normalized[:len(normalized)-len(suffix)], "%f", &number); err != nil {
			fail("invalid --rate: %s", rate)
		}
	}

	pps := number
	switch suffix {
	case "pps":
	case "kbps", "k":
		pps = number * 1_000.0 / float64(payloadBytes*8)
	case "mbps", "m":
		pps = number * 1_000_000.0 / float64(payloadBytes*8)
	case "gbps", "g":
		pps = number * 1_000_000_000.0 / float64(payloadBytes*8)
	default:
		fail("invalid --rate: %s", rate)
	}

	if pps <= 0 {
		fail("invalid --rate: %s", rate)
	}

	interval := time.Duration(float64(time.Second) / pps)
	if interval <= 0 {
		interval = time.Nanosecond
	}
	return rateSpec{
		pps:       pps,
		interval:  interval,
		totalPkts: int(math.Ceil(durationS * pps)),
	}
}

func runHash(runID string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(runID))
	return h.Sum64()
}

func fillPayload(payload []byte, runIDHash uint64, seq uint64, scheduledRelNS int64, sendRelNS int64, sendUnixNS int64) {
	binary.BigEndian.PutUint32(payload[0:4], dataMagic)
	binary.BigEndian.PutUint16(payload[4:6], version)
	binary.BigEndian.PutUint16(payload[6:8], headerLen)
	binary.BigEndian.PutUint64(payload[8:16], runIDHash)
	binary.BigEndian.PutUint64(payload[16:24], seq)
	binary.BigEndian.PutUint64(payload[24:32], uint64(scheduledRelNS))
	binary.BigEndian.PutUint64(payload[32:40], uint64(sendRelNS))
	binary.BigEndian.PutUint64(payload[40:48], uint64(sendUnixNS))
}

func fmtSeconds(d time.Duration) string {
	return fmt.Sprintf("%.9f", d.Seconds())
}

func fmtMillis(d time.Duration) string {
	return fmt.Sprintf("%.6f", float64(d.Nanoseconds())/1e6)
}

func median(values []int64) int64 {
	if len(values) == 0 {
		return 0
	}
	sorted := append([]int64(nil), values...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	return sorted[len(sorted)/2]
}

func clockSync(conn *net.UDPConn) (int64, int64, int) {
	offsets := make([]int64, 0, 5)
	rtts := make([]int64, 0, 5)
	buffer := make([]byte, 256)
	for index := 0; index < 5; index++ {
		t1 := time.Now().UnixNano()
		request := fmt.Sprintf("UWLP_CLOCK_REQ client_unix_ns=%d\n", t1)
		if _, err := conn.Write([]byte(request)); err != nil {
			continue
		}
		if err := conn.SetReadDeadline(time.Now().Add(500 * time.Millisecond)); err != nil {
			continue
		}
		n, err := conn.Read(buffer)
		t4 := time.Now().UnixNano()
		if err != nil {
			continue
		}
		var echoedT1, t2, t3 int64
		if _, err := fmt.Sscanf(string(buffer[:n]), "UWLP_CLOCK_ACK client_unix_ns=%d server_recv_unix_ns=%d server_send_unix_ns=%d", &echoedT1, &t2, &t3); err != nil || echoedT1 != t1 {
			continue
		}
		offsets = append(offsets, ((t2-t1)+(t3-t4))/2)
		rtts = append(rtts, (t4-t1)-(t3-t2))
	}
	_ = conn.SetReadDeadline(time.Time{})
	return median(offsets), median(rtts), len(offsets)
}

func writeSummary(cfg config, spec rateSpec, attempted int, sent int, failed int, start time.Time, end time.Time, deadlineReached bool, serverMinusPhoneNS int64, clockRTTNS int64, clockSamples int, doneAck bool) error {
	elapsed := end.Sub(start)
	actualPayloadMbps := 0.0
	if elapsed > 0 {
		actualPayloadMbps = float64(sent) * float64(cfg.payloadBytes) * 8 / elapsed.Seconds() / 1e6
	}
	senderCSV := filepath.Join(cfg.outDir, "uplink_udp_sender.csv.gz")
	if cfg.noCSV {
		senderCSV = "disabled"
	}
	lines := []string{
		fmt.Sprintf("run_id=%s", cfg.runID),
		fmt.Sprintf("target_server=%s", cfg.target),
		fmt.Sprintf("port=%d", cfg.port),
		fmt.Sprintf("rate=%s", cfg.rate),
		fmt.Sprintf("pps=%.9f", spec.pps),
		fmt.Sprintf("payload_bytes=%d", cfg.payloadBytes),
		fmt.Sprintf("duration_s=%.6f", cfg.durationS),
		fmt.Sprintf("duration_mode=%s", cfg.durationMode),
		fmt.Sprintf("scheduled_packets=%d", spec.totalPkts),
		fmt.Sprintf("attempted_packets=%d", attempted),
		fmt.Sprintf("sent_packets=%d", sent),
		fmt.Sprintf("send_failed_packets=%d", failed),
		fmt.Sprintf("elapsed_s=%.9f", elapsed.Seconds()),
		fmt.Sprintf("sender_elapsed_s=%.9f", elapsed.Seconds()),
		fmt.Sprintf("actual_payload_mbps=%.9f", actualPayloadMbps),
		fmt.Sprintf("deadline_reached=%t", deadlineReached),
		fmt.Sprintf("clock_sync_samples=%d", clockSamples),
		fmt.Sprintf("done_ack=%t", doneAck),
		fmt.Sprintf("clock_server_minus_phone_ms=%.6f", float64(serverMinusPhoneNS)/1e6),
		fmt.Sprintf("clock_offset_ms=%.6f", -float64(serverMinusPhoneNS)/1e6),
		fmt.Sprintf("clock_rtt_ms=%.6f", float64(clockRTTNS)/1e6),
		fmt.Sprintf("sender_csv=%s", senderCSV),
		fmt.Sprintf("sender_progress=%s", filepath.Join(cfg.outDir, "uplink_udp_sender_progress.tsv")),
		fmt.Sprintf("progress_interval_s=%.6f", cfg.progressInterval.Seconds()),
	}
	text := ""
	for _, line := range lines {
		text += line + "\n"
	}
	return os.WriteFile(filepath.Join(cfg.outDir, "uplink_udp_sender_summary.txt"), []byte(text), 0644)
}

func main() {
	cfg := parseArgs()
	if cfg.showVersion {
		fmt.Println(appName)
		return
	}

	spec := parseRate(cfg.rate, cfg.payloadBytes, cfg.durationS)
	if err := os.MkdirAll(cfg.outDir, 0755); err != nil {
		fail("create output directory failed: %v", err)
	}

	serverAddr, err := net.ResolveUDPAddr("udp", fmt.Sprintf("%s:%d", cfg.target, cfg.port))
	if err != nil {
		fail("resolve target failed: %v", err)
	}

	conn, err := net.DialUDP("udp", nil, serverAddr)
	if err != nil {
		fail("dial UDP target failed: %v", err)
	}
	defer conn.Close()
	serverMinusPhoneNS, clockRTTNS, clockSamples := clockSync(conn)

	var file *os.File
	var gzipWriter *gzip.Writer
	var writer *csv.Writer
	if !cfg.noCSV {
		csvPath := filepath.Join(cfg.outDir, "uplink_udp_sender.csv.gz")
		file, err = os.Create(csvPath)
		if err != nil {
			fail("create CSV failed: %v", err)
		}
		gzipWriter = gzip.NewWriter(file)
		writer = csv.NewWriter(gzipWriter)
		header := []string{
			"run_id",
			"target_server",
			"port",
			"rate",
			"pps",
			"payload_bytes",
			"seq",
			"scheduled_send_rel_s",
			"send_rel_s",
			"send_unix_ns",
			"send_epoch_s",
			"send_lag_ms",
		}
		if err := writer.Write(header); err != nil {
			fail("write CSV header failed: %v", err)
		}
	}

	start := time.Now().Add(200 * time.Millisecond)
	ridHash := runHash(cfg.runID)
	attemptedPackets := 0
	sentPackets := 0
	failedPackets := 0
	var attemptedAtomic int64
	var sentAtomic int64
	var failedAtomic int64
	var lastSeqAtomic int64
	var lastSendRelNSAtomic int64
	var lastSendLagNSAtomic int64
	deadline := start.Add(time.Duration(cfg.durationS * float64(time.Second)))
	payload := make([]byte, cfg.payloadBytes)

	progressPath := filepath.Join(cfg.outDir, "uplink_udp_sender_progress.tsv")
	progressFile, err := os.Create(progressPath)
	if err != nil {
		fail("create progress file failed: %v", err)
	}
	progressWriter := bufio.NewWriterSize(progressFile, 4096)
	if _, err := progressWriter.WriteString("event\ttimestamp_unix_ns\telapsed_s\tscheduled_packets\tattempted_packets\tsent_packets\tsend_failed_packets\tlast_seq\tlast_send_rel_s\tsend_lag_ms\n"); err != nil {
		fail("write progress header failed: %v", err)
	}

	var progressMu sync.Mutex
	var progressErrMu sync.Mutex
	var progressErr error
	setProgressErr := func(err error) {
		progressErrMu.Lock()
		if progressErr == nil {
			progressErr = err
		}
		progressErrMu.Unlock()
	}
	recordProgress := func(event string, at time.Time) error {
		elapsed := at.Sub(start).Seconds()
		if elapsed < 0 {
			elapsed = 0
		}
		due := int64(math.Floor(elapsed * spec.pps))
		if event == "final" {
			due = int64(spec.totalPkts)
		}
		if due < 0 {
			due = 0
		}
		if due > int64(spec.totalPkts) {
			due = int64(spec.totalPkts)
		}
		lastSendRelNS := atomic.LoadInt64(&lastSendRelNSAtomic)
		lastSendLagNS := atomic.LoadInt64(&lastSendLagNSAtomic)
		lastSendRelS := ""
		if lastSendRelNS > 0 {
			lastSendRelS = fmt.Sprintf("%.9f", float64(lastSendRelNS)/1e9)
		}
		line := fmt.Sprintf(
			"%s\t%d\t%.9f\t%d\t%d\t%d\t%d\t%d\t%s\t%.6f\n",
			event,
			at.UnixNano(),
			elapsed,
			due,
			atomic.LoadInt64(&attemptedAtomic),
			atomic.LoadInt64(&sentAtomic),
			atomic.LoadInt64(&failedAtomic),
			atomic.LoadInt64(&lastSeqAtomic),
			lastSendRelS,
			float64(lastSendLagNS)/1e6,
		)
		progressMu.Lock()
		defer progressMu.Unlock()
		if _, err := progressWriter.WriteString(line); err != nil {
			return err
		}
		if err := progressWriter.Flush(); err != nil {
			return err
		}
		if event == "final" {
			if err := progressFile.Sync(); err != nil {
				return err
			}
		}
		fmt.Printf(
			"PROGRESS event=%s timestamp_unix_ns=%d elapsed_s=%.9f scheduled_packets=%d attempted_packets=%d sent_packets=%d send_failed_packets=%d last_seq=%d last_send_rel_s=%s send_lag_ms=%.6f\n",
			event,
			at.UnixNano(),
			elapsed,
			due,
			atomic.LoadInt64(&attemptedAtomic),
			atomic.LoadInt64(&sentAtomic),
			atomic.LoadInt64(&failedAtomic),
			atomic.LoadInt64(&lastSeqAtomic),
			lastSendRelS,
			float64(lastSendLagNS)/1e6,
		)
		return nil
	}
	if err := recordProgress("start", time.Now()); err != nil {
		_ = progressFile.Close()
		fail("write initial progress failed: %v", err)
	}
	progressStop := make(chan struct{})
	var progressWG sync.WaitGroup
	progressWG.Add(1)
	go func() {
		defer progressWG.Done()
		ticker := time.NewTicker(cfg.progressInterval)
		defer ticker.Stop()
		for {
			select {
			case at := <-ticker.C:
				if err := recordProgress("heartbeat", at); err != nil {
					setProgressErr(err)
					return
				}
			case <-progressStop:
				return
			}
		}
	}()
	for index := 0; ; index++ {
		if cfg.durationMode == "packet-count" && index >= spec.totalPkts {
			break
		}
		if cfg.durationMode == "wall-clock" && !time.Now().Before(deadline) {
			break
		}
		seq := uint64(index + 1)
		scheduled := start.Add(time.Duration(float64(index) / spec.pps * float64(time.Second)))
		if delay := time.Until(scheduled); delay > 0 {
			if cfg.durationMode == "wall-clock" {
				remaining := time.Until(deadline)
				if remaining <= 0 {
					break
				}
				if delay > remaining {
					time.Sleep(remaining)
					break
				}
			}
			time.Sleep(delay)
		}
		if cfg.durationMode == "wall-clock" && !time.Now().Before(deadline) {
			break
		}

		sendTime := time.Now()
		scheduledRel := scheduled.Sub(start)
		sendRel := sendTime.Sub(start)
		sendUnixNS := sendTime.UnixNano()
		attemptedPackets++
		atomic.AddInt64(&attemptedAtomic, 1)
		atomic.StoreInt64(&lastSeqAtomic, int64(seq))
		atomic.StoreInt64(&lastSendRelNSAtomic, sendRel.Nanoseconds())
		atomic.StoreInt64(&lastSendLagNSAtomic, sendTime.Sub(scheduled).Nanoseconds())
		fillPayload(payload, ridHash, seq, scheduledRel.Nanoseconds(), sendRel.Nanoseconds(), sendUnixNS)
		if _, err := conn.Write(payload); err != nil {
			failedPackets++
			atomic.AddInt64(&failedAtomic, 1)
			fmt.Fprintf(os.Stderr, "WARNING: send seq=%d failed: %v\n", seq, err)
		} else {
			sentPackets++
			atomic.AddInt64(&sentAtomic, 1)
		}

		if !cfg.noCSV {
			record := []string{
				cfg.runID,
				cfg.target,
				strconv.Itoa(cfg.port),
				cfg.rate,
				fmt.Sprintf("%.9f", spec.pps),
				strconv.Itoa(cfg.payloadBytes),
				strconv.FormatUint(seq, 10),
				fmtSeconds(scheduledRel),
				fmtSeconds(sendRel),
				strconv.FormatInt(sendUnixNS, 10),
				fmt.Sprintf("%.9f", float64(sendUnixNS)/1e9),
				fmtMillis(sendTime.Sub(scheduled)),
			}
			if err := writer.Write(record); err != nil {
				fail("write CSV row failed: %v", err)
			}
		}
	}
	doneAck := false
	donePayload := []byte(fmt.Sprintf("UWLP_DONE run_id=%s last_seq=%d sent_packets=%d\n", cfg.runID, sentPackets, sentPackets))
	ackBuffer := make([]byte, 512)
	for attempt := 0; attempt < 20 && !doneAck; attempt++ {
		_, _ = conn.Write(donePayload)
		_ = conn.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
		n, readErr := conn.Read(ackBuffer)
		if readErr == nil && strings.HasPrefix(string(ackBuffer[:n]), "UWLP_DONE_ACK ") {
			doneAck = true
		}
	}
	_ = conn.SetReadDeadline(time.Time{})
	close(progressStop)
	progressWG.Wait()
	if err := recordProgress("final", time.Now()); err != nil {
		setProgressErr(err)
	}
	progressMu.Lock()
	if err := progressWriter.Flush(); err != nil {
		setProgressErr(err)
	}
	if err := progressFile.Sync(); err != nil {
		setProgressErr(err)
	}
	if err := progressFile.Close(); err != nil {
		setProgressErr(err)
	}
	progressMu.Unlock()
	progressErrMu.Lock()
	progressWriteErr := progressErr
	progressErrMu.Unlock()
	if progressWriteErr != nil {
		fail("write progress failed: %v", progressWriteErr)
	}

	if !cfg.noCSV {
		writer.Flush()
		if err := writer.Error(); err != nil {
			fail("flush CSV failed: %v", err)
		}
		if err := gzipWriter.Close(); err != nil {
			fail("close gzip failed: %v", err)
		}
		if err := file.Close(); err != nil {
			fail("close CSV file failed: %v", err)
		}
	}

	end := time.Now()
	deadlineReached := end.Equal(deadline) || end.After(deadline)
	if err := writeSummary(cfg, spec, attemptedPackets, sentPackets, failedPackets, start, end, deadlineReached, serverMinusPhoneNS, clockRTTNS, clockSamples, doneAck); err != nil {
		fail("write summary failed: %v", err)
	}

	fmt.Printf("run_id=%s\n", cfg.runID)
	fmt.Printf("target=%s:%d\n", cfg.target, cfg.port)
	fmt.Printf("pps=%.9f\n", spec.pps)
	fmt.Printf("sent_packets=%d\n", sentPackets)
	if !cfg.noCSV {
		fmt.Printf("sender_csv=%s\n", filepath.Join(cfg.outDir, "uplink_udp_sender.csv.gz"))
	}
	fmt.Printf("sender_progress=%s\n", filepath.Join(cfg.outDir, "uplink_udp_sender_progress.tsv"))
}
