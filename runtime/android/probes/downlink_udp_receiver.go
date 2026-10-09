package main

import (
	"bufio"
	"encoding/binary"
	"encoding/csv"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

const (
	dataMagic              uint32 = 0x44574C50 // "DWLP"
	version                uint16 = 2
	headerLen                     = 48
	legacyVersion                 = 1
	legacyHeaderLen               = 40
	appName                       = "downlink_udp_receiver 5"
	defaultClockSamples           = 5
	compactHeaderSize             = 64
	compactRecordSize             = 48
	compactCaptureBytes           = 256
	defaultRecvBufferBytes        = 64 * 1024 * 1024
	soTimestampNS                 = 35
	soRXQOverflow                 = 40
	msgDontWait                   = 0x40
	msgTrunc                      = 0x20
)

type config struct {
	runID            string
	server           string
	port             int
	durationS        float64
	timeoutS         float64
	waitTimeoutS     float64
	keepaliveS       float64
	payloadBytes     int
	outDir           string
	outputMode       string
	precisionMode    string
	windowMS         float64
	progressInterval time.Duration
	gapMS            float64
	showVersion      bool
	recvBufferBytes  int
	receiveAPI       string
	receiveBatchSize int
}

type compactRecord struct {
	seq             uint64
	scheduledSendNS uint64
	sendNS          uint64
	sendUnixNS      uint64
	recvUnixNS      int64
	socketDrops     uint32
	payloadBytes    uint16
	protocolVersion uint16
}

type packet struct {
	seq             uint64
	scheduledSendNS uint64
	sendNS          uint64
	sendUnixNS      uint64
	payloadBytes    int
	recvRelS        float64
	recvUnixNS      int64
	duplicate       bool
	remoteAddr      string
}

type sequenceTracker struct {
	words []uint64
}

// rawMMsg matches Linux struct mmsghdr on the supported Android and Linux ABIs.
// syscall.Msghdr already has the target architecture's pointer layout.
type rawMMsg struct {
	hdr    syscall.Msghdr
	length uint32
}

func recvmmsgSyscallNumber() uintptr {
	switch runtime.GOARCH {
	case "386":
		return 337
	case "arm":
		return 365
	case "arm64":
		return 243
	case "amd64":
		return 299
	default:
		return 0
	}
}

func recvmmsgBatch(conn *net.UDPConn, payloads [][]byte, oobs [][]byte, msgs []rawMMsg, iovs []syscall.Iovec, truncate bool) (int, error) {
	if recvmmsgSyscallNumber() == 0 {
		return 0, syscall.ENOSYS
	}
	raw, err := conn.SyscallConn()
	if err != nil {
		return 0, err
	}
	count := 0
	var callErr error
	err = raw.Read(func(fd uintptr) bool {
		for i := range msgs {
			iovs[i] = syscall.Iovec{Base: &payloads[i][0]}
			iovs[i].SetLen(len(payloads[i]))
			msgs[i].hdr = syscall.Msghdr{Iov: &iovs[i], Iovlen: 1, Control: &oobs[i][0]}
			msgs[i].hdr.SetControllen(len(oobs[i]))
			msgs[i].length = 0
		}
		flags := uintptr(msgDontWait)
		if truncate {
			flags |= msgTrunc
		}
		r1, _, errno := syscall.RawSyscall6(recvmmsgSyscallNumber(), fd, uintptr(unsafe.Pointer(&msgs[0])), uintptr(len(msgs)), flags, 0, 0)
		if errno != 0 {
			if errno == syscall.EAGAIN || errno == syscall.EWOULDBLOCK {
				return false
			}
			callErr = errno
			return true
		}
		count = int(r1)
		return true
	})
	if err != nil {
		return 0, err
	}
	return count, callErr
}

func (tracker *sequenceTracker) mark(seq uint64) bool {
	if seq == 0 {
		return false
	}
	wordIndex := int((seq - 1) / 64)
	for len(tracker.words) <= wordIndex {
		tracker.words = append(tracker.words, 0)
	}
	mask := uint64(1) << ((seq - 1) % 64)
	duplicate := tracker.words[wordIndex]&mask != 0
	tracker.words[wordIndex] |= mask
	return duplicate
}

func parseArgs() config {
	cfg := config{}
	flag.StringVar(&cfg.runID, "run-id", "", "run identifier written into outputs")
	flag.StringVar(&cfg.server, "server", "", "server host/IP that is waiting for registration")
	flag.IntVar(&cfg.port, "port", 5003, "server UDP port")
	flag.Float64Var(&cfg.durationS, "duration-s", 150, "receive duration after the first data packet")
	flag.Float64Var(&cfg.timeoutS, "timeout-s", 5, "extra wait after duration")
	flag.Float64Var(&cfg.waitTimeoutS, "wait-timeout-s", 120, "maximum wait for clock sync and first data")
	flag.Float64Var(&cfg.keepaliveS, "keepalive-s", 10, "same-socket keepalive interval")
	flag.IntVar(&cfg.payloadBytes, "payload-bytes", 64, "expected UDP payload bytes")
	flag.StringVar(&cfg.outDir, "out-dir", ".", "output directory")
	flag.StringVar(&cfg.outputMode, "output-mode", "raw", "output mode: raw, arrival, or latency")
	flag.StringVar(&cfg.precisionMode, "precision-mode", "trace", "timestamp mode: trace or measurement")
	flag.Float64Var(&cfg.windowMS, "window-ms", 10, "latency aggregation window in milliseconds")
	flag.DurationVar(&cfg.progressInterval, "progress-interval", time.Second, "progress heartbeat interval")
	flag.Float64Var(&cfg.gapMS, "gap-ms", 100, "minimum receive gap to report in milliseconds")
	flag.BoolVar(&cfg.showVersion, "version", false, "print version and exit")
	flag.IntVar(&cfg.recvBufferBytes, "recv-buffer-bytes", defaultRecvBufferBytes, "requested UDP receive buffer")
	flag.StringVar(&cfg.receiveAPI, "receive-api", "auto", "receive API: auto, readmsg, or recvmmsg")
	flag.IntVar(&cfg.receiveBatchSize, "receive-batch-size", 64, "recvmmsg batch size")
	flag.Parse()

	if cfg.showVersion {
		return cfg
	}
	if cfg.runID == "" {
		cfg.runID = fmt.Sprintf("downlink_udp_loss_%d", time.Now().Unix())
	}
	if cfg.server == "" {
		fail("missing --server")
	}
	if cfg.port <= 0 || cfg.port > 65535 {
		fail("invalid --port: %d", cfg.port)
	}
	if cfg.durationS <= 0 || cfg.timeoutS < 0 || cfg.waitTimeoutS <= 0 || cfg.keepaliveS <= 0 {
		fail("duration/wait/keepalive values are invalid")
	}
	if cfg.payloadBytes < headerLen {
		fail("--payload-bytes must be at least %d", headerLen)
	}
	if cfg.outputMode != "raw" && cfg.outputMode != "arrival" && cfg.outputMode != "latency" && cfg.outputMode != "both" {
		fail("--output-mode must be raw, arrival, latency, or both")
	}
	if cfg.precisionMode != "trace" && cfg.precisionMode != "measurement" {
		fail("--precision-mode must be trace or measurement")
	}
	if cfg.windowMS <= 0 || cfg.progressInterval <= 0 || cfg.gapMS <= 0 {
		fail("window/progress/gap values must be positive")
	}
	if cfg.outputMode == "latency" && int64(cfg.windowMS*1e6) == 0 {
		fail("--window-ms is below one nanosecond")
	}
	if cfg.recvBufferBytes < 65536 {
		fail("--recv-buffer-bytes must be at least 65536")
	}
	if cfg.receiveAPI != "auto" && cfg.receiveAPI != "readmsg" && cfg.receiveAPI != "recvmmsg" {
		fail("--receive-api must be auto, readmsg, or recvmmsg")
	}
	if cfg.receiveBatchSize < 1 || cfg.receiveBatchSize > 256 {
		fail("--receive-batch-size must be between 1 and 256")
	}
	if cfg.receiveAPI == "recvmmsg" && !linuxSocketPlatform() {
		fail("--receive-api recvmmsg requires Linux or Android")
	}
	return cfg
}

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "ERROR: "+format+"\n", args...)
	os.Exit(2)
}

func resolveReceiveAPI(requested, outputMode string) string {
	if requested != "auto" {
		return requested
	}
	if outputMode != "raw" && linuxSocketPlatform() {
		return "recvmmsg"
	}
	return "readmsg"
}

func linuxSocketPlatform() bool {
	return runtime.GOOS == "linux" || runtime.GOOS == "android"
}

func datagramCopyLength(wireLen, bufferLen int) (int, bool) {
	if wireLen < 0 {
		return 0, false
	}
	if bufferLen < 0 {
		bufferLen = 0
	}
	if wireLen <= bufferLen {
		return wireLen, false
	}
	return bufferLen, true
}

func parsePacket(payload []byte) (uint64, uint64, uint64, uint64, bool) {
	if len(payload) < legacyHeaderLen || binary.BigEndian.Uint32(payload[0:4]) != dataMagic {
		return 0, 0, 0, 0, false
	}
	packetVersion := binary.BigEndian.Uint16(payload[4:6])
	declaredHeaderLen := int(binary.BigEndian.Uint16(payload[6:8]))
	if packetVersion != version && packetVersion != legacyVersion {
		return 0, 0, 0, 0, false
	}
	minimumHeaderLen := legacyHeaderLen
	if packetVersion == version {
		minimumHeaderLen = headerLen
	}
	if declaredHeaderLen < minimumHeaderLen || len(payload) < minimumHeaderLen {
		return 0, 0, 0, 0, false
	}
	seq := binary.BigEndian.Uint64(payload[16:24])
	scheduledSendNS := binary.BigEndian.Uint64(payload[24:32])
	sendNS := binary.BigEndian.Uint64(payload[32:40])
	var sendUnixNS uint64
	if packetVersion == version {
		sendUnixNS = binary.BigEndian.Uint64(payload[40:48])
	}
	return seq, scheduledSendNS, sendNS, sendUnixNS, true
}

func controlPayload(kind string, cfg config, index int) []byte {
	return []byte(fmt.Sprintf(
		"%s run_id=%s version=%d payload_bytes=%d duration_s=%.6f keepalive_s=%.6f index=%d client_unix_ns=%d\n",
		kind, cfg.runID, version, cfg.payloadBytes, cfg.durationS, cfg.keepaliveS, index, time.Now().UnixNano(),
	))
}

func parseHelloAck(payload []byte) (map[string]int64, bool) {
	parts := strings.Fields(string(payload))
	if len(parts) == 0 || parts[0] != "DWLP_HELLO_ACK" {
		return nil, false
	}
	fields := make(map[string]int64)
	for _, token := range parts[1:] {
		key, value, ok := strings.Cut(token, "=")
		if !ok {
			continue
		}
		if parsed, err := strconv.ParseInt(value, 10, 64); err == nil {
			fields[key] = parsed
		}
	}
	return fields, true
}

func sendControl(conn *net.UDPConn, kind string, cfg config, index int) {
	if _, err := conn.Write(controlPayload(kind, cfg, index)); err != nil {
		fmt.Fprintf(os.Stderr, "WARNING: send %s failed: %v\n", kind, err)
	}
}

func runControlSender(conn *net.UDPConn, cfg config, clockReady, dataStarted, stop <-chan struct{}, done chan<- struct{}) {
	defer close(done)
	ticker := time.NewTicker(200 * time.Millisecond)
	defer ticker.Stop()
	keepaliveTicker := time.NewTicker(time.Duration(cfg.keepaliveS * float64(time.Second)))
	defer keepaliveTicker.Stop()
	helloIndex := 0
	readyIndex := 0
	keepaliveIndex := 0
	ready := false
	started := false
	sendControl(conn, "DWLP_HELLO", cfg, helloIndex)
	helloIndex++
	for {
		select {
		case <-stop:
			return
		case <-clockReady:
			ready = true
			clockReady = nil
			sendControl(conn, "DWLP_READY", cfg, readyIndex)
			readyIndex++
		case <-dataStarted:
			started = true
			dataStarted = nil
		case <-ticker.C:
			if !ready {
				sendControl(conn, "DWLP_HELLO", cfg, helloIndex)
				helloIndex++
			} else if !started {
				sendControl(conn, "DWLP_READY", cfg, readyIndex)
				readyIndex++
			}
		case <-keepaliveTicker.C:
			sendControl(conn, "DWLP_KEEPALIVE", cfg, keepaliveIndex)
			keepaliveIndex++
		}
	}
}

func medianInt64(values []int64) int64 {
	if len(values) == 0 {
		return 0
	}
	sorted := append([]int64(nil), values...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	return sorted[len(sorted)/2]
}

func roundHalfAwayFromZero(value float64) int64 {
	if value < 0 {
		return int64(math.Ceil(value - 0.5))
	}
	return int64(math.Floor(value + 0.5))
}

func writeCSV(cfg config, packets []packet) error {
	path := filepath.Join(cfg.outDir, "downlink_udp_receiver.csv")
	file, err := os.Create(path)
	if err != nil {
		return err
	}
	defer file.Close()
	writer := csv.NewWriter(file)
	defer writer.Flush()
	if err := writer.Write([]string{"run_id", "port", "seq", "scheduled_send_rel_s", "send_rel_s", "recv_rel_s", "send_unix_ns", "send_epoch_s", "recv_unix_ns", "recv_epoch_s", "payload_bytes", "duplicate", "remote_addr"}); err != nil {
		return err
	}
	for _, row := range packets {
		record := []string{
			cfg.runID, strconv.Itoa(cfg.port), strconv.FormatUint(row.seq, 10),
			fmt.Sprintf("%.9f", float64(row.scheduledSendNS)/1e9), fmt.Sprintf("%.9f", float64(row.sendNS)/1e9), fmt.Sprintf("%.9f", row.recvRelS),
			strconv.FormatUint(row.sendUnixNS, 10), fmt.Sprintf("%.9f", float64(row.sendUnixNS)/1e9),
			strconv.FormatInt(row.recvUnixNS, 10), fmt.Sprintf("%.9f", float64(row.recvUnixNS)/1e9),
			strconv.Itoa(row.payloadBytes), strconv.FormatBool(row.duplicate), row.remoteAddr,
		}
		if err := writer.Write(record); err != nil {
			return err
		}
	}
	return writer.Error()
}

func writeKeyValues(path string, values [][2]string) error {
	file, err := os.Create(path)
	if err != nil {
		return err
	}
	defer file.Close()
	writer := bufio.NewWriter(file)
	for _, item := range values {
		if _, err := fmt.Fprintf(writer, "%s=%s\n", item[0], item[1]); err != nil {
			return err
		}
	}
	return writer.Flush()
}

func configureReceiveSocket(conn *net.UDPConn, requested int) (int, error) {
	raw, err := conn.SyscallConn()
	if err != nil {
		return 0, err
	}
	var socketErr error
	actual := 0
	err = raw.Control(func(fd uintptr) {
		if e := syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF, requested); e != nil {
			socketErr = e
			return
		}
		if e := syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, soTimestampNS, 1); e != nil {
			socketErr = e
			return
		}
		if e := syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, soRXQOverflow, 1); e != nil {
			socketErr = e
			return
		}
		actual, socketErr = syscall.GetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF)
	})
	if err != nil {
		return 0, err
	}
	return actual, socketErr
}

func parseSocketMetadata(oob []byte) (int64, uint32, bool, bool) {
	messages, err := syscall.ParseSocketControlMessage(oob)
	if err != nil {
		return 0, 0, false, false
	}
	var timestamp int64
	var drops uint32
	haveTimestamp, haveDrops := false, false
	for _, message := range messages {
		if message.Header.Level != syscall.SOL_SOCKET {
			continue
		}
		switch message.Header.Type {
		case soTimestampNS:
			if len(message.Data) >= 8 {
				if len(message.Data) >= 16 {
					timestamp = int64(binary.NativeEndian.Uint64(message.Data[:8]))*1e9 + int64(binary.NativeEndian.Uint64(message.Data[8:16]))
				} else {
					timestamp = int64(int32(binary.NativeEndian.Uint32(message.Data[:4])))*1e9 + int64(int32(binary.NativeEndian.Uint32(message.Data[4:8])))
				}
				haveTimestamp = true
			}
		case soRXQOverflow:
			if len(message.Data) >= 4 {
				drops = binary.NativeEndian.Uint32(message.Data[:4])
				haveDrops = true
			}
		}
	}
	return timestamp, drops, haveTimestamp, haveDrops
}

func writeCaptureHeader(writer *bufio.Writer, cfg config) error {
	header := make([]byte, compactHeaderSize)
	copy(header[:4], []byte("UTRC"))
	binary.BigEndian.PutUint16(header[4:6], 1)
	binary.BigEndian.PutUint16(header[6:8], compactHeaderSize)
	binary.BigEndian.PutUint16(header[8:10], compactRecordSize)
	header[10] = 2 // downlink
	if cfg.outputMode == "arrival" {
		header[11] = 1
	} else {
		header[11] = 2
	}
	binary.BigEndian.PutUint32(header[12:16], uint32(cfg.payloadBytes))
	_, err := writer.Write(header)
	return err
}

func writeCaptureRecord(writer *bufio.Writer, record compactRecord) error {
	var row [compactRecordSize]byte
	binary.BigEndian.PutUint64(row[0:8], record.seq)
	binary.BigEndian.PutUint64(row[8:16], record.scheduledSendNS)
	binary.BigEndian.PutUint64(row[16:24], record.sendNS)
	binary.BigEndian.PutUint64(row[24:32], record.sendUnixNS)
	binary.BigEndian.PutUint64(row[32:40], uint64(record.recvUnixNS))
	binary.BigEndian.PutUint32(row[40:44], record.socketDrops)
	binary.BigEndian.PutUint16(row[44:46], record.payloadBytes)
	binary.BigEndian.PutUint16(row[46:48], record.protocolVersion)
	_, err := writer.Write(row[:])
	return err
}

func readCaptureRecord(reader *bufio.Reader) (compactRecord, error) {
	var row [compactRecordSize]byte
	if _, err := io.ReadFull(reader, row[:]); err != nil {
		return compactRecord{}, err
	}
	return compactRecord{
		seq: binary.BigEndian.Uint64(row[0:8]), scheduledSendNS: binary.BigEndian.Uint64(row[8:16]),
		sendNS: binary.BigEndian.Uint64(row[16:24]), sendUnixNS: binary.BigEndian.Uint64(row[24:32]),
		recvUnixNS: int64(binary.BigEndian.Uint64(row[32:40])), socketDrops: binary.BigEndian.Uint32(row[40:44]),
		payloadBytes: binary.BigEndian.Uint16(row[44:46]), protocolVersion: binary.BigEndian.Uint16(row[46:48]),
	}, nil
}

func convertCapture(cfg config, capturePath, gapPath string, firstRecvNS, clockOffsetNS int64) (int, int, int, int, int, int64, int64, error) {
	file, err := os.Open(capturePath)
	if err != nil {
		return 0, 0, 0, 0, 0, -1, -1, err
	}
	defer file.Close()
	reader := bufio.NewReaderSize(file, 4*1024*1024)
	header := make([]byte, compactHeaderSize)
	if _, err = io.ReadFull(reader, header); err != nil || string(header[:4]) != "UTRC" {
		return 0, 0, 0, 0, 0, -1, -1, fmt.Errorf("invalid UTRC header")
	}
	gapFile, err := os.Create(gapPath)
	if err != nil {
		return 0, 0, 0, 0, 0, -1, -1, err
	}
	gapWriter := bufio.NewWriter(gapFile)
	fmt.Fprintln(gapWriter, "gap_index\tstart_recv_unix_ns\tend_recv_unix_ns\tstart_recv_epoch_s\tend_recv_epoch_s\tgap_ms\tprev_seq\tnext_seq\tseq_jump\tprev_send_rel_s\tnext_send_rel_s\tsend_advance_s\tsocket_drop_delta")
	var arrivalWriter, delayWriter, timeWriter *bufio.Writer
	var preciseArrivalWriter, preciseLatencyWriter, preciseWindowWriter *bufio.Writer
	var arrivalFile, delayFile, timeFile *os.File
	var preciseArrivalFile, preciseLatencyFile, preciseWindowFile *os.File
	if cfg.outputMode == "arrival" || cfg.outputMode == "both" {
		arrivalFile, err = os.Create(filepath.Join(cfg.outDir, "udp_arrival_time_ms.txt"))
		if err != nil {
			return 0, 0, 0, 0, 0, -1, -1, err
		}
		arrivalWriter = bufio.NewWriterSize(arrivalFile, 4*1024*1024)
	}
	if cfg.outputMode == "latency" || cfg.outputMode == "both" {
		delayFile, err = os.Create(filepath.Join(cfg.outDir, "delay.txt"))
		if err != nil {
			return 0, 0, 0, 0, 0, -1, -1, err
		}
		timeFile, err = os.Create(filepath.Join(cfg.outDir, "delay_window_time_ms.txt"))
		if err != nil {
			return 0, 0, 0, 0, 0, -1, -1, err
		}
		delayWriter, timeWriter = bufio.NewWriter(delayFile), bufio.NewWriter(timeFile)
	}
	if cfg.precisionMode == "measurement" && cfg.outputMode != "raw" {
		preciseArrivalFile, err = os.Create(filepath.Join(cfg.outDir, "udp_arrival_time_precise.tsv"))
		if err != nil {
			return 0, 0, 0, 0, -1, -1, -1, err
		}
		preciseArrivalWriter = bufio.NewWriterSize(preciseArrivalFile, 4*1024*1024)
		fmt.Fprintln(preciseArrivalWriter, "seq\tscheduled_send_rel_s\tsend_rel_s\tsend_unix_ns\treceive_unix_ns\treceive_rel_s\tpayload_bytes\tsocket_drops")
		if cfg.outputMode == "latency" || cfg.outputMode == "both" {
			preciseLatencyFile, err = os.Create(filepath.Join(cfg.outDir, "udp_latency_precise.tsv"))
			if err != nil {
				return 0, 0, 0, 0, -1, -1, -1, err
			}
			preciseWindowFile, err = os.Create(filepath.Join(cfg.outDir, "delay_precise.tsv"))
			if err != nil {
				return 0, 0, 0, 0, -1, -1, -1, err
			}
			preciseLatencyWriter = bufio.NewWriterSize(preciseLatencyFile, 4*1024*1024)
			preciseWindowWriter = bufio.NewWriter(preciseWindowFile)
			fmt.Fprintln(preciseLatencyWriter, "seq\tsend_unix_ns\treceive_unix_ns\treceive_rel_s\tdelay_ms\tpayload_bytes")
			fmt.Fprintln(preciseWindowWriter, "window_start_ms\tmean_delay_ms\tsample_count")
		}
	}
	defer func() {
		gapWriter.Flush()
		gapFile.Close()
		if arrivalFile != nil {
			arrivalWriter.Flush()
			arrivalFile.Close()
		}
		if delayFile != nil {
			delayWriter.Flush()
			timeWriter.Flush()
			delayFile.Close()
			timeFile.Close()
		}
		if preciseArrivalFile != nil {
			preciseArrivalWriter.Flush()
			preciseArrivalFile.Close()
		}
		if preciseLatencyFile != nil {
			preciseLatencyWriter.Flush()
			preciseWindowWriter.Flush()
			preciseLatencyFile.Close()
			preciseWindowFile.Close()
		}
	}()
	type aggregate struct {
		sum   int64
		count int64
	}
	aggregates := make(map[int64]aggregate)
	var previous compactRecord
	havePrevious := false
	gapIndex := 0
	records := 0
	lastArrivalMS := int64(0)
	for {
		record, readErr := readCaptureRecord(reader)
		if readErr == io.EOF || readErr == io.ErrUnexpectedEOF {
			break
		}
		if readErr != nil {
			return records, 0, 0, 0, gapIndex, -1, -1, readErr
		}
		records++
		if havePrevious {
			gapMS := float64(record.recvUnixNS-previous.recvUnixNS) / 1e6
			if gapMS >= cfg.gapMS {
				gapIndex++
				fmt.Fprintf(gapWriter, "%d\t%d\t%d\t%.9f\t%.9f\t%.3f\t%d\t%d\t%d\t%.9f\t%.9f\t%.9f\t%d\n", gapIndex, previous.recvUnixNS, record.recvUnixNS, float64(previous.recvUnixNS)/1e9, float64(record.recvUnixNS)/1e9, gapMS, previous.seq, record.seq, int64(record.seq)-int64(previous.seq), float64(previous.sendNS)/1e9, float64(record.sendNS)/1e9, float64(int64(record.sendNS)-int64(previous.sendNS))/1e9, int64(record.socketDrops)-int64(previous.socketDrops))
			}
		}
		previous, havePrevious = record, true
		if cfg.outputMode == "arrival" || cfg.outputMode == "both" {
			arrivalMS := int64(math.Floor(float64(record.recvUnixNS-firstRecvNS) / 1e6))
			if records > 1 && arrivalMS < lastArrivalMS {
				arrivalMS = lastArrivalMS
			}
			fmt.Fprintln(arrivalWriter, arrivalMS)
			lastArrivalMS = arrivalMS
		}
		if preciseArrivalWriter != nil {
			fmt.Fprintf(preciseArrivalWriter, "%d\t%.9f\t%.9f\t%d\t%d\t%.9f\t%d\t%d\n", record.seq, float64(record.scheduledSendNS)/1e9, float64(record.sendNS)/1e9, record.sendUnixNS, record.recvUnixNS, float64(record.recvUnixNS-firstRecvNS)/1e9, record.payloadBytes, record.socketDrops)
		}
		if (cfg.outputMode == "latency" || cfg.outputMode == "both") && record.sendUnixNS != 0 {
			index := (record.recvUnixNS - firstRecvNS) / int64(cfg.windowMS*1e6)
			item := aggregates[index]
			item.sum += record.recvUnixNS - int64(record.sendUnixNS) + clockOffsetNS
			item.count++
			aggregates[index] = item
			if preciseLatencyWriter != nil {
				delayMS := float64(record.recvUnixNS-int64(record.sendUnixNS)+clockOffsetNS) / 1e6
				fmt.Fprintf(preciseLatencyWriter, "%d\t%d\t%d\t%.9f\t%.9f\t%d\n", record.seq, record.sendUnixNS, record.recvUnixNS, float64(record.recvUnixNS-firstRecvNS)/1e9, delayMS, record.payloadBytes)
			}
		}
	}
	if cfg.outputMode == "arrival" {
		return records, records, 0, 0, gapIndex, -1, -1, nil
	}
	indices := make([]int64, 0, len(aggregates))
	for index := range aggregates {
		indices = append(indices, index)
	}
	sort.Slice(indices, func(i, j int) bool { return indices[i] < indices[j] })
	empty := 0
	for i := 1; i < len(indices); i++ {
		empty += int(indices[i] - indices[i-1] - 1)
	}
	completed := make(map[int64]float64, len(aggregates))
	for _, index := range indices {
		item := aggregates[index]
		completed[index] = float64(item.sum) / float64(item.count) / 1e6
	}
	if preciseWindowWriter != nil {
		for _, index := range indices {
			item := aggregates[index]
			fmt.Fprintf(preciseWindowWriter, "%.9f\t%.9f\t%d\n", float64(index)*cfg.windowMS, float64(item.sum)/float64(item.count)/1e6, item.count)
		}
	}
	if cfg.outputMode == "both" && len(indices) >= 2 {
		for i := 1; i < len(indices); i++ {
			left, right := indices[i-1], indices[i]
			if right == left+1 {
				continue
			}
			step := (completed[right] - completed[left]) / float64(right-left)
			for index := left + 1; index < right; index++ {
				completed[index] = completed[left] + step*float64(index-left)
			}
		}
		indices = indices[:0]
		for index := range completed {
			indices = append(indices, index)
		}
		sort.Slice(indices, func(i, j int) bool { return indices[i] < indices[j] })
	}
	for _, index := range indices {
		fmt.Fprintln(delayWriter, roundHalfAwayFromZero(completed[index]))
		fmt.Fprintln(timeWriter, strconv.FormatFloat(float64(index)*cfg.windowMS, 'f', -1, 64))
	}
	firstIndex, lastIndex := int64(-1), int64(-1)
	if len(indices) > 0 {
		firstIndex, lastIndex = indices[0], indices[len(indices)-1]
	}
	if cfg.outputMode == "both" {
		if err := writeBandwidthTrace(cfg, capturePath, firstRecvNS); err != nil {
			return records, 0, len(indices), empty, gapIndex, firstIndex, lastIndex, err
		}
		data, _ := json.MarshalIndent(map[string]any{
			"source": "downlink_phone_utrc", "direction": "downlink", "packet_count": records,
			"capture_duration_ms": float64(previous.recvUnixNS-firstRecvNS) / 1e6,
			"latency": map[string]any{
				"window_ms": cfg.windowMS, "observed_delay_windows": len(aggregates),
				"interpolated_delay_windows": len(indices) - len(aggregates), "raw_empty_windows": empty,
				"completed_delay_windows": len(indices), "delay_interpolation": "linear",
			},
		}, "", "  ")
		if err := os.WriteFile(filepath.Join(cfg.outDir, "custom_udp_trace_summary.json"), append(data, '\n'), 0644); err != nil {
			return records, 0, len(indices), empty, gapIndex, firstIndex, lastIndex, err
		}
	}
	return records, 0, len(indices), empty, gapIndex, firstIndex, lastIndex, nil
}

func writeBandwidthTrace(cfg config, capturePath string, firstRecvNS int64) error {
	file, err := os.Open(capturePath)
	if err != nil {
		return err
	}
	defer file.Close()
	reader := bufio.NewReaderSize(file, 4*1024*1024)
	header := make([]byte, compactHeaderSize)
	if _, err := io.ReadFull(reader, header); err != nil {
		return err
	}
	type bucket struct{ packets, bytes int }
	buckets := make(map[int64]bucket)
	lastMS := int64(0)
	for {
		record, readErr := readCaptureRecord(reader)
		if readErr == io.EOF || readErr == io.ErrUnexpectedEOF {
			break
		}
		if readErr != nil {
			return readErr
		}
		rel := (record.recvUnixNS - firstRecvNS) / 1e6
		if rel < 0 {
			rel = 0
		}
		if rel > lastMS {
			lastMS = rel
		}
		item := buckets[rel]
		item.packets++
		item.bytes += int(record.payloadBytes)
		buckets[rel] = item
	}
	output, err := os.Create(filepath.Join(cfg.outDir, "bandwidth_trace.tsv"))
	if err != nil {
		return err
	}
	defer output.Close()
	writer := bufio.NewWriterSize(output, 4*1024*1024)
	fmt.Fprintln(writer, "time_ms\tpacket_count\tpayload_bytes\twindow_packet_count\twindow_payload_bytes\tbandwidth_mbps")
	windowMS := int64(math.Max(1, math.Round(cfg.windowMS)))
	rollingPackets, rollingBytes := 0, 0
	for timeMS := int64(0); timeMS <= lastMS; timeMS++ {
		current := buckets[timeMS]
		rollingPackets += current.packets
		rollingBytes += current.bytes
		expired := buckets[timeMS-windowMS]
		rollingPackets -= expired.packets
		rollingBytes -= expired.bytes
		fmt.Fprintf(writer, "%d\t%d\t%d\t%d\t%d\t%.9f\n", timeMS, current.packets, current.bytes, rollingPackets, rollingBytes, float64(rollingBytes*8)/(float64(windowMS)*1000))
	}
	return writer.Flush()
}

func main() {
	cfg := parseArgs()
	if cfg.showVersion {
		fmt.Println(appName)
		return
	}
	receiveAPI := resolveReceiveAPI(cfg.receiveAPI, cfg.outputMode)
	receiveAPIFallback := ""
	if err := os.MkdirAll(cfg.outDir, 0755); err != nil {
		fail("create output directory failed: %v", err)
	}
	serverAddr, err := net.ResolveUDPAddr("udp", fmt.Sprintf("%s:%d", cfg.server, cfg.port))
	if err != nil {
		fail("resolve server failed: %v", err)
	}
	conn, err := net.DialUDP("udp", nil, serverAddr)
	if err != nil {
		fail("dial UDP server failed: %v", err)
	}
	defer conn.Close()
	actualRecvBuffer, err := configureReceiveSocket(conn, cfg.recvBufferBytes)
	if err != nil && cfg.outputMode != "raw" && linuxSocketPlatform() {
		fail("configure kernel receive metadata failed: %v", err)
	}

	progressFile, err := os.Create(filepath.Join(cfg.outDir, "downlink_udp_receiver_progress.tsv"))
	if err != nil {
		fail("create progress failed: %v", err)
	}
	progressWriter := bufio.NewWriter(progressFile)
	fmt.Fprintln(progressWriter, "event\ttimestamp_unix_ns\telapsed_s\treceived_packets\tlast_seq\tlast_packet_recv_unix_ns\tsocket_timeout_count")
	progressWriter.Flush()
	defer progressFile.Close()

	gapFile, err := os.Create(filepath.Join(cfg.outDir, "udp_receive_gap_report.tsv"))
	if err != nil {
		fail("create gap report failed: %v", err)
	}
	gapWriter := bufio.NewWriter(gapFile)
	fmt.Fprintln(gapWriter, "gap_index\tstart_recv_unix_ns\tend_recv_unix_ns\tstart_recv_epoch_s\tend_recv_epoch_s\tgap_ms\tprev_seq\tnext_seq\tseq_jump\tprev_send_rel_s\tnext_send_rel_s\tsend_advance_s")
	defer gapFile.Close()

	var captureFile *os.File
	var captureWriter *bufio.Writer
	capturePath := filepath.Join(cfg.outDir, "udp_trace.utrc")
	if cfg.outputMode != "raw" {
		captureFile, err = os.Create(capturePath)
		if err == nil {
			captureWriter = bufio.NewWriterSize(captureFile, 4*1024*1024)
			err = writeCaptureHeader(captureWriter, cfg)
		}
	}
	if err != nil {
		fail("create compact capture failed: %v", err)
	}
	if captureFile != nil {
		defer captureFile.Close()
	}

	clockReady := make(chan struct{})
	dataStarted := make(chan struct{})
	stopControl := make(chan struct{})
	doneControl := make(chan struct{})
	go runControlSender(conn, cfg, clockReady, dataStarted, stopControl, doneControl)
	var clockReadyOnce, dataStartedOnce sync.Once

	tracker := sequenceTracker{}
	packets := make([]packet, 0)
	clockOffsets := make([]int64, 0, defaultClockSamples)
	clockRtts := make([]int64, 0, defaultClockSamples)
	var clockOffsetNS int64
	ackIndices := make(map[int64]bool)
	buf := make([]byte, 65535)
	oob := make([]byte, 256)
	waitDeadline := time.Now().Add(time.Duration(cfg.waitTimeoutS * float64(time.Second)))
	progressStart := time.Now()
	nextProgress := progressStart
	var firstDataTime time.Time
	var firstRecvUnixNS int64
	var receivedPackets, uniquePackets, duplicatePackets, socketTimeoutCount int
	var firstSeq, lastSeq uint64
	var firstRecvRelS, lastRecvRelS float64
	var payloadMin, payloadMax int
	var lastPacketRecvUnixNS int64
	gapCount := 0
	skippedDelayRows := 0
	delayWindows, firstWindow, lastWindow := 0, int64(-1), int64(-1)
	emptyWindows := 0
	missingKernelTimestamps, missingDropMetadata := 0, 0
	maxSocketDrops := uint32(0)
	captureBackpressure := 0
	var maxAppReadLagNS int64
	appReadLagSamples := make([]int64, 0, 4096)
	batchCalls, batchPackets, batchMin, batchMax, batchFull := 0, 0, 0, 0, 0
	doneReceived := false
	var doneLastSeq uint64
	headerOnlyPackets := 0
	controlTruncatedPackets := 0

	writeProgress := func(event string, now time.Time) {
		fmt.Fprintf(progressWriter, "%s\t%d\t%.9f\t%d\t%d\t%d\t%d\n", event, now.UnixNano(), now.Sub(progressStart).Seconds(), receivedPackets, lastSeq, lastPacketRecvUnixNS, socketTimeoutCount)
		progressWriter.Flush()
		gapWriter.Flush()
		nextProgress = now.Add(cfg.progressInterval)
	}
	writeProgress("start", progressStart)
	_ = conn.SetReadDeadline(waitDeadline)
	oldGCPercent := 100
	if cfg.outputMode != "raw" && receiveAPI != "recvmmsg" {
		oldGCPercent = debug.SetGCPercent(-1)
	}
	var batchPayloads [][]byte
	var batchOOBs [][]byte
	var batchMsgs []rawMMsg
	var batchIOVs []syscall.Iovec
	if receiveAPI == "recvmmsg" {
		batchPayloadBytes := 65535
		if cfg.outputMode != "raw" {
			batchPayloadBytes = compactCaptureBytes
		}
		batchPayloads = make([][]byte, cfg.receiveBatchSize)
		batchOOBs = make([][]byte, cfg.receiveBatchSize)
		batchMsgs = make([]rawMMsg, cfg.receiveBatchSize)
		batchIOVs = make([]syscall.Iovec, cfg.receiveBatchSize)
		for i := range batchPayloads {
			batchPayloads[i] = make([]byte, batchPayloadBytes)
			batchOOBs[i] = make([]byte, 256)
		}
	}
	processDatagram := func(packetBuf []byte, oobBuf []byte, copiedLen, wireLen, oobn int, userReadTime time.Time) {
		if copiedLen < 0 {
			copiedLen = 0
		}
		if copiedLen > len(packetBuf) {
			copiedLen = len(packetBuf)
		}
		if wireLen < copiedLen {
			wireLen = copiedLen
		}
		wasTruncated := wireLen > copiedLen
		if oobn < 0 {
			oobn = 0
		}
		if oobn > len(oobBuf) {
			oobn = len(oobBuf)
		}
		kernelNS, socketDrops, haveKernelTimestamp, haveDropMetadata := parseSocketMetadata(oobBuf[:oobn])
		requireKernelMetadata := cfg.outputMode != "raw" && linuxSocketPlatform()
		if requireKernelMetadata && !haveKernelTimestamp {
			missingKernelTimestamps++
			return
		}
		_ = haveDropMetadata
		recvTime := userReadTime
		if haveKernelTimestamp {
			recvTime = time.Unix(0, kernelNS)
		}
		isData := false
		if copiedLen >= legacyHeaderLen && binary.BigEndian.Uint32(packetBuf[:4]) == dataMagic {
			packetVersion := binary.BigEndian.Uint16(packetBuf[4:6])
			declaredHeaderLen := int(binary.BigEndian.Uint16(packetBuf[6:8]))
			isData = (packetVersion == legacyVersion && declaredHeaderLen >= legacyHeaderLen) ||
				(packetVersion == version && declaredHeaderLen >= headerLen)
		}
		if !isData {
			if wireLen > copiedLen && strings.HasPrefix(string(packetBuf[:copiedLen]), "DWLP_") {
				controlTruncatedPackets++
				return
			}
			if fields, isAck := parseHelloAck(packetBuf[:copiedLen]); isAck {
				t1, ok1 := fields["client_unix_ns"]
				t2, ok2 := fields["server_recv_unix_ns"]
				t3, ok3 := fields["server_send_unix_ns"]
				index, okIndex := fields["index"]
				if ok1 && ok2 && ok3 && okIndex && !ackIndices[index] && len(clockOffsets) < defaultClockSamples {
					ackIndices[index] = true
					t4 := recvTime.UnixNano()
					clockOffsets = append(clockOffsets, ((t2-t1)+(t3-t4))/2)
					clockRtts = append(clockRtts, (t4-t1)-(t3-t2))
					if len(clockOffsets) == defaultClockSamples {
						clockOffsetNS = medianInt64(clockOffsets)
						clockReadyOnce.Do(func() { close(clockReady) })
					}
				}
				return
			}
			if strings.HasPrefix(string(packetBuf[:copiedLen]), "DWLP_DONE ") {
				for _, token := range strings.Fields(string(packetBuf[:copiedLen]))[1:] {
					key, value, found := strings.Cut(token, "=")
					if found && key == "last_seq" {
						doneLastSeq, _ = strconv.ParseUint(value, 10, 64)
					}
				}
				doneReceived = true
				_, _ = conn.Write([]byte(fmt.Sprintf("DWLP_DONE_ACK run_id=%s received_packets=%d socket_drops=%d\n", cfg.runID, receivedPackets, maxSocketDrops)))
				return
			}
			return
		}
		seq, scheduledSendNS, sendNS, sendUnixNS, ok := parsePacket(packetBuf[:copiedLen])
		if !ok || len(clockOffsets) < defaultClockSamples {
			return
		}
		if firstDataTime.IsZero() {
			firstDataTime = recvTime
			dataStartedOnce.Do(func() { close(dataStarted) })
			if cfg.outputMode != "raw" {
				_ = conn.SetReadDeadline(recvTime.Add(time.Duration((cfg.durationS + cfg.timeoutS) * float64(time.Second))))
			}
		}
		recvRelS := recvTime.Sub(firstDataTime).Seconds()
		recvUnixNS := recvTime.UnixNano()
		duplicate := false
		if receiveAPI == "readmsg" || cfg.outputMode == "raw" {
			duplicate = tracker.mark(seq)
			if duplicate {
				duplicatePackets++
			} else {
				uniquePackets++
			}
		} else {
			uniquePackets++
		}
		receivedPackets++
		if haveKernelTimestamp && receivedPackets%1024 == 0 {
			lag := userReadTime.UnixNano() - kernelNS
			appReadLagSamples = append(appReadLagSamples, lag)
			if lag > maxAppReadLagNS {
				maxAppReadLagNS = lag
			}
		}
		lastPacketRecvUnixNS = recvUnixNS
		if receivedPackets == 1 {
			firstRecvUnixNS = recvUnixNS
		}
		if firstSeq == 0 || seq < firstSeq {
			firstSeq = seq
		}
		if seq > lastSeq {
			lastSeq = seq
		}
		if receivedPackets == 1 {
			firstRecvRelS, payloadMin, payloadMax = recvRelS, wireLen, wireLen
		} else {
			if wireLen < payloadMin {
				payloadMin = wireLen
			}
			if wireLen > payloadMax {
				payloadMax = wireLen
			}
		}
		lastRecvRelS = recvRelS
		if socketDrops > maxSocketDrops {
			maxSocketDrops = socketDrops
		}
		if receiveAPI == "recvmmsg" && cfg.outputMode != "raw" && wasTruncated {
			headerOnlyPackets++
		}
		if cfg.outputMode == "raw" {
			packets = append(packets, packet{seq, scheduledSendNS, sendNS, sendUnixNS, wireLen, recvRelS, recvUnixNS, duplicate, conn.RemoteAddr().String()})
		} else if wireLen > math.MaxUint16 {
			fail("UDP datagram length %d exceeds compact record limit", wireLen)
		} else if err := writeCaptureRecord(captureWriter, compactRecord{seq, scheduledSendNS, sendNS, sendUnixNS, recvUnixNS, socketDrops, uint16(wireLen), version}); err != nil {
			captureBackpressure++
		}
	}

	for {
		now := time.Now()
		if firstDataTime.IsZero() {
			if now.After(waitDeadline) {
				close(stopControl)
				<-doneControl
				fail("timed out waiting %.3fs for synchronized downlink data", cfg.waitTimeoutS)
			}
		} else if now.After(firstDataTime.Add(time.Duration((cfg.durationS + cfg.timeoutS) * float64(time.Second)))) {
			break
		}
		if !now.Before(nextProgress) {
			writeProgress("heartbeat", now)
		}
		if receiveAPI == "recvmmsg" {
			count, batchErr := recvmmsgBatch(conn, batchPayloads, batchOOBs, batchMsgs, batchIOVs, cfg.outputMode != "raw")
			batchCalls++
			if batchErr == syscall.EAGAIN || batchErr == syscall.EWOULDBLOCK {
				continue
			}
			if batchErr == syscall.ECONNREFUSED {
				if doneReceived {
					break
				}
				continue
			}
			if netErr, ok := batchErr.(net.Error); ok && netErr.Timeout() {
				socketTimeoutCount++
				continue
			}
			if batchErr == syscall.ENOSYS && cfg.receiveAPI == "auto" {
				receiveAPI = "readmsg"
				receiveAPIFallback = "recvmmsg_unsupported"
				continue
			}
			if batchErr != nil {
				fail("recvmmsg failed: %v", batchErr)
			}
			if count > 0 {
				batchPackets += count
				if batchMin == 0 || count < batchMin {
					batchMin = count
				}
				if count > batchMax {
					batchMax = count
				}
				if count == cfg.receiveBatchSize {
					batchFull++
				}
				nowRead := time.Now()
				for i := 0; i < count; i++ {
					wireLen := int(batchMsgs[i].length)
					copiedLen, _ := datagramCopyLength(wireLen, len(batchPayloads[i]))
					processDatagram(batchPayloads[i], batchOOBs[i], copiedLen, wireLen, int(batchMsgs[i].hdr.Controllen), nowRead)
				}
			}
			continue
		}
		n, oobn, _, _, readErr := conn.ReadMsgUDP(buf, oob)
		if readErr != nil {
			if netErr, ok := readErr.(net.Error); ok && netErr.Timeout() {
				socketTimeoutCount++
				continue
			}
			fmt.Fprintf(os.Stderr, "WARNING: receive failed: %v\n", readErr)
			continue
		}
		processDatagram(buf, oob, n, n, oobn, time.Now())
	}

	close(stopControl)
	<-doneControl
	if cfg.outputMode != "raw" && receiveAPI != "recvmmsg" {
		debug.SetGCPercent(oldGCPercent)
	}
	if captureWriter != nil {
		if err := captureWriter.Flush(); err != nil {
			fail("flush compact capture failed: %v", err)
		}
		if err := captureFile.Close(); err != nil {
			fail("close compact capture failed: %v", err)
		}
	}
	if cfg.outputMode != "raw" {
		var arrivalRows, delayRows, timeRows, empty int
		arrivalRows, delayRows, timeRows, empty, gapCount, firstWindow, lastWindow, err = convertCapture(cfg, capturePath, filepath.Join(cfg.outDir, "udp_receive_gap_report.tsv"), firstRecvUnixNS, clockOffsetNS)
		if err != nil {
			fail("postprocess compact capture failed: %v", err)
		}
		_ = arrivalRows
		_ = delayRows
		if cfg.outputMode == "latency" || cfg.outputMode == "both" {
			delayWindows = timeRows
		}
		emptyWindows = empty
		_ = os.Remove(capturePath)
	}
	if err := gapWriter.Flush(); err != nil {
		fail("flush gap report failed: %v", err)
	}
	writeProgress("final", time.Now())
	if cfg.outputMode == "raw" {
		if err := writeCSV(cfg, packets); err != nil {
			fail("write CSV failed: %v", err)
		}
	}

	offsetNS := clockOffsetNS
	rttNS := medianInt64(clockRtts)
	lagPercentileMS := func(percentile float64) string {
		if len(appReadLagSamples) == 0 {
			return "0.000000"
		}
		sorted := append([]int64(nil), appReadLagSamples...)
		sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
		index := int(math.Ceil(percentile*float64(len(sorted)))) - 1
		if index < 0 {
			index = 0
		}
		if index >= len(sorted) {
			index = len(sorted) - 1
		}
		return fmt.Sprintf("%.6f", float64(sorted[index])/1e6)
	}
	batchMean := 0.0
	if batchCalls > 0 {
		batchMean = float64(batchPackets) / float64(batchCalls)
	}
	summary := [][2]string{
		{"run_id", cfg.runID}, {"server", cfg.server}, {"server_addr", serverAddr.String()}, {"port", strconv.Itoa(cfg.port)},
		{"duration_s", fmt.Sprintf("%.6f", cfg.durationS)}, {"timeout_s", fmt.Sprintf("%.6f", cfg.timeoutS)}, {"wait_timeout_s", fmt.Sprintf("%.6f", cfg.waitTimeoutS)},
		{"keepalive_s", fmt.Sprintf("%.6f", cfg.keepaliveS)}, {"payload_bytes", strconv.Itoa(cfg.payloadBytes)}, {"payload_bytes_min", strconv.Itoa(payloadMin)}, {"payload_bytes_max", strconv.Itoa(payloadMax)},
		{"protocol_version", strconv.Itoa(int(version))}, {"header_len", strconv.Itoa(headerLen)}, {"output_mode", cfg.outputMode}, {"window_ms", strconv.FormatFloat(cfg.windowMS, 'f', -1, 64)},
		{"timestamp_fields", "send_unix_ns,recv_unix_ns"}, {"clock_sync_samples", strconv.Itoa(len(clockOffsets))}, {"clock_offset_ms", fmt.Sprintf("%.6f", float64(offsetNS)/1e6)}, {"clock_rtt_ms", fmt.Sprintf("%.6f", float64(rttNS)/1e6)},
		{"timestamp_source", func() string {
			if cfg.outputMode == "raw" {
				return "userspace"
			}
			return "kernel_so_timestampns"
		}()}, {"requested_rcvbuf_bytes", strconv.Itoa(cfg.recvBufferBytes)}, {"actual_rcvbuf_bytes", strconv.Itoa(actualRecvBuffer)},
		{"kernel_timestamp_packets", strconv.Itoa(receivedPackets - missingKernelTimestamps)}, {"missing_kernel_timestamp_packets", strconv.Itoa(missingKernelTimestamps)}, {"missing_drop_metadata_packets", strconv.Itoa(missingDropMetadata)}, {"socket_rxq_overflow_packets", strconv.FormatUint(uint64(maxSocketDrops), 10)}, {"capture_backpressure_records", strconv.Itoa(captureBackpressure)},
		{"capture_records", strconv.Itoa(receivedPackets - captureBackpressure)}, {"app_read_lag_ms_p50", lagPercentileMS(0.50)}, {"app_read_lag_ms_p95", lagPercentileMS(0.95)}, {"app_read_lag_ms_p99", lagPercentileMS(0.99)}, {"app_read_lag_ms_max", fmt.Sprintf("%.6f", float64(maxAppReadLagNS)/1e6)},
		{"receive_api", receiveAPI}, {"receive_api_requested", cfg.receiveAPI}, {"receive_api_effective", receiveAPI}, {"receive_api_fallback", receiveAPIFallback}, {"receive_capture_bytes", strconv.Itoa(func() int {
			if receiveAPI == "recvmmsg" && cfg.outputMode != "raw" {
				return compactCaptureBytes
			}
			return len(buf)
		}())}, {"header_only_packets", strconv.Itoa(headerOnlyPackets)}, {"control_truncated_packets", strconv.Itoa(controlTruncatedPackets)}, {"payload_bytes_source", "wire_length"},
		{"receive_batch_size", strconv.Itoa(cfg.receiveBatchSize)}, {"recvmmsg_calls", strconv.Itoa(batchCalls)}, {"recvmmsg_packets", strconv.Itoa(batchPackets)},
		{"batch_packets_min", strconv.Itoa(batchMin)}, {"batch_packets_max", strconv.Itoa(batchMax)}, {"batch_full_count", strconv.Itoa(batchFull)},
		{"receive_syscall_packets_mean", fmt.Sprintf("%.6f", batchMean)},
		{"done_received", strconv.FormatBool(doneReceived)}, {"done_last_seq", strconv.FormatUint(doneLastSeq, 10)},
		{"receiver_integrity", func() string {
			if cfg.outputMode != "raw" && linuxSocketPlatform() && (!doneReceived || missingKernelTimestamps > 0 || maxSocketDrops > 0 || captureBackpressure > 0 || controlTruncatedPackets > 0) {
				return "contaminated"
			}
			return "clean"
		}()},
		{"received_packets", strconv.Itoa(receivedPackets)}, {"unique_received_packets", strconv.Itoa(uniquePackets)}, {"duplicate_packets", strconv.Itoa(duplicatePackets)},
		{"payload_bytes_total", strconv.FormatInt(int64(receivedPackets)*int64(payloadMin), 10)},
		{"first_seq", strconv.FormatUint(firstSeq, 10)}, {"last_seq", strconv.FormatUint(lastSeq, 10)}, {"first_recv_rel_s", fmt.Sprintf("%.9f", firstRecvRelS)}, {"last_recv_rel_s", fmt.Sprintf("%.9f", lastRecvRelS)},
		{"socket_timeout_count", strconv.Itoa(socketTimeoutCount)}, {"receive_gap_rows", strconv.Itoa(gapCount)},
	}
	if err := writeKeyValues(filepath.Join(cfg.outDir, "downlink_udp_receiver_summary.txt"), summary); err != nil {
		fail("write summary failed: %v", err)
	}
	if cfg.outputMode == "latency" || cfg.outputMode == "both" {
		if cfg.outputMode != "both" && firstWindow >= 0 {
			emptyWindows = int(lastWindow-firstWindow+1) - delayWindows
		}
		delaySummary := [][2]string{
			{"source", "phone_inline"}, {"valid_delay_rows", strconv.Itoa(receivedPackets - skippedDelayRows)}, {"delay_windows", strconv.Itoa(delayWindows)},
			{"observed_delay_windows", strconv.Itoa(delayWindows - emptyWindows)}, {"interpolated_delay_windows", strconv.Itoa(func() int {
				if cfg.outputMode == "both" {
					return emptyWindows
				}
				return 0
			}())}, {"raw_empty_windows", strconv.Itoa(emptyWindows)}, {"completed_delay_windows", strconv.Itoa(delayWindows)}, {"delay_interpolation", func() string {
				if cfg.outputMode == "both" {
					return "linear"
				}
				return "none"
			}()},
			{"first_window_index", strconv.FormatInt(firstWindow, 10)}, {"last_window_index", strconv.FormatInt(lastWindow, 10)}, {"empty_windows", strconv.Itoa(emptyWindows)}, {"skipped_rows", strconv.Itoa(skippedDelayRows)},
			{"first_recv_rel_ms", fmt.Sprintf("%.6f", firstRecvRelS*1000)}, {"last_recv_rel_ms", fmt.Sprintf("%.6f", lastRecvRelS*1000)},
			{"clock_offset_ms", fmt.Sprintf("%.6f", float64(offsetNS)/1e6)}, {"clock_sync_samples", strconv.Itoa(len(clockOffsets))},
		}
		if err := writeKeyValues(filepath.Join(cfg.outDir, "delay_summary.txt"), delaySummary); err != nil {
			fail("write delay summary failed: %v", err)
		}
	}
	fmt.Printf("run_id=%s\nreceived_packets=%d\noutput_mode=%s\n", cfg.runID, receivedPackets, cfg.outputMode)
}
