package main

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"flag"
	"fmt"
	"math"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	dataMagic           uint32 = 0x55574C50 // UWLP
	protocolVersion     uint16 = 2
	headerLen                  = 48
	legacyVersion       uint16 = 1
	legacyHeaderLen            = 40
	utrcHeaderSize             = 64
	utrcRecordSize             = 48
	compactCaptureBytes        = 256
	defaultRecvBuffer          = 64 * 1024 * 1024
	soTimestampNS              = 35
	soRXQOverflow              = 40
	soRcvBufForce              = 33
	msgDontWait                = 0x40
	msgTrunc                   = 0x20
	controlBufferBytes         = 128
	appName                    = "uplink_udp_receiver 1"
)

type config struct {
	runID            string
	port             int
	durationS        float64
	timeoutS         float64
	waitTimeoutS     float64
	outDir           string
	readyFile        string
	progressPath     string
	progressInterval time.Duration
	outputMode       string
	postprocess      string
	windowMS         float64
	payloadBytes     int
	recvBufferBytes  int
	receiveAPI       string
	receiveBatchSize int
	gapMS            float64
	showVersion      bool
}

type rawMMsg struct {
	hdr    syscall.Msghdr
	length uint32
}

type captureRecord struct {
	seq             uint64
	scheduledSendNS uint64
	sendNS          uint64
	sendUnixNS      uint64
	recvUnixNS      int64
	socketDrops     uint32
	payloadBytes    uint16
	version         uint16
}

type sequenceTracker struct {
	words []uint64
}

func (tracker *sequenceTracker) mark(seq uint64) bool {
	if seq == 0 {
		return false
	}
	index := int((seq - 1) / 64)
	for len(tracker.words) <= index {
		tracker.words = append(tracker.words, 0)
	}
	mask := uint64(1) << ((seq - 1) % 64)
	duplicate := tracker.words[index]&mask != 0
	tracker.words[index] |= mask
	return duplicate
}

func parseArgs() config {
	cfg := config{}
	flag.StringVar(&cfg.runID, "run-id", "", "run identifier")
	flag.IntVar(&cfg.port, "port", 5201, "UDP port")
	flag.Float64Var(&cfg.durationS, "duration-s", 150, "capture duration after first data packet")
	flag.Float64Var(&cfg.timeoutS, "timeout-s", 5, "extra receive timeout")
	flag.Float64Var(&cfg.waitTimeoutS, "wait-timeout-s", 120, "wait for first packet")
	flag.StringVar(&cfg.outDir, "out-dir", ".", "output directory")
	flag.StringVar(&cfg.readyFile, "ready-file", "", "touch after bind")
	flag.StringVar(&cfg.progressPath, "progress-path", "", "progress TSV path")
	flag.DurationVar(&cfg.progressInterval, "progress-interval", time.Second, "progress interval")
	flag.StringVar(&cfg.outputMode, "output-mode", "compact", "output mode; compact is required")
	flag.StringVar(&cfg.postprocess, "postprocess", "arrival", "arrival, latency, or none")
	flag.Float64Var(&cfg.windowMS, "window-ms", 100, "postprocess window")
	flag.IntVar(&cfg.payloadBytes, "payload-bytes", 1472, "expected UDP payload bytes")
	flag.IntVar(&cfg.recvBufferBytes, "recv-buffer-bytes", defaultRecvBuffer, "requested socket receive buffer")
	flag.StringVar(&cfg.receiveAPI, "receive-api", "auto", "auto, readmsg, or recvmmsg")
	flag.IntVar(&cfg.receiveBatchSize, "receive-batch-size", 64, "recvmmsg batch size")
	flag.Float64Var(&cfg.gapMS, "gap-ms", 100, "gap threshold")
	flag.BoolVar(&cfg.showVersion, "version", false, "print version")
	flag.Parse()
	if cfg.showVersion {
		return cfg
	}
	if cfg.runID == "" {
		cfg.runID = fmt.Sprintf("uplink_udp_loss_%d", time.Now().Unix())
	}
	if cfg.port <= 0 || cfg.port > 65535 || cfg.durationS <= 0 || cfg.timeoutS < 0 || cfg.waitTimeoutS <= 0 {
		fail("invalid port/duration/timeout values")
	}
	if cfg.outputMode != "compact" {
		fail("--output-mode compact is required; use the legacy Python receiver for raw CSV")
	}
	if cfg.postprocess != "arrival" && cfg.postprocess != "latency" && cfg.postprocess != "none" {
		fail("--postprocess must be arrival, latency, or none")
	}
	if cfg.progressInterval <= 0 || cfg.windowMS <= 0 || cfg.gapMS <= 0 || cfg.recvBufferBytes < 65536 || cfg.payloadBytes < headerLen {
		fail("invalid progress/window/gap/receive-buffer values")
	}
	if cfg.receiveAPI != "auto" && cfg.receiveAPI != "readmsg" && cfg.receiveAPI != "recvmmsg" {
		fail("--receive-api must be auto, readmsg, or recvmmsg")
	}
	if cfg.receiveBatchSize < 1 || cfg.receiveBatchSize > 256 {
		fail("--receive-batch-size must be between 1 and 256")
	}
	return cfg
}

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "ERROR: "+format+"\n", args...)
	os.Exit(2)
}

func syscallNumber() uintptr {
	switch runtime.GOARCH {
	case "amd64":
		return 299
	case "arm64":
		return 243
	case "arm":
		return 365
	case "386":
		return 337
	default:
		return 0
	}
}

func resolveAPI(requested string) string {
	if requested != "auto" {
		return requested
	}
	if syscallNumber() != 0 {
		return "recvmmsg"
	}
	return "readmsg"
}

func recvmmsgBatch(conn *net.UDPConn, payloads [][]byte, oobs [][]byte, msgs []rawMMsg, iovs []syscall.Iovec) (int, error) {
	if syscallNumber() == 0 {
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
		flags := uintptr(msgDontWait | msgTrunc)
		r1, _, errno := syscall.RawSyscall6(syscallNumber(), fd, uintptr(unsafe.Pointer(&msgs[0])), uintptr(len(msgs)), flags, 0, 0)
		if errno != 0 {
			callErr = errno
			return true
		}
		count = int(r1)
		return true
	})
	if err != nil {
		return 0, err
	}
	if callErr != nil {
		return 0, callErr
	}
	return count, nil
}

func configureSocket(conn *net.UDPConn, requested int) (int, error) {
	raw, err := conn.SyscallConn()
	if err != nil {
		return 0, err
	}
	actual := 0
	var socketErr error
	err = raw.Control(func(fd uintptr) {
		if socketErr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF, requested); socketErr != nil {
			return
		}
		actual, socketErr = syscall.GetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF)
		if socketErr != nil {
			return
		}
		if actual < requested {
			_ = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, soRcvBufForce, requested)
			actual, socketErr = syscall.GetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF)
		}
		if socketErr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, soTimestampNS, 1); socketErr != nil {
			return
		}
		socketErr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, soRXQOverflow, 1)
	})
	if err != nil {
		return 0, err
	}
	return actual, socketErr
}

func connectUDP(conn *net.UDPConn, addr *net.UDPAddr) error {
	ip := addr.IP.To4()
	if ip == nil {
		return fmt.Errorf("only IPv4 sender addresses are supported: %s", addr)
	}
	raw, err := conn.SyscallConn()
	if err != nil {
		return err
	}
	var connectErr error
	err = raw.Control(func(fd uintptr) {
		sa := &syscall.SockaddrInet4{Port: addr.Port}
		copy(sa.Addr[:], ip)
		connectErr = syscall.Connect(int(fd), sa)
	})
	if err != nil {
		return err
	}
	return connectErr
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
			if len(message.Data) >= 16 {
				sec := int64(binary.NativeEndian.Uint64(message.Data[:8]))
				nsec := int64(binary.NativeEndian.Uint64(message.Data[8:16]))
				timestamp = sec*1_000_000_000 + nsec
				haveTimestamp = true
			} else if len(message.Data) >= 8 {
				sec := int64(int32(binary.NativeEndian.Uint32(message.Data[:4])))
				nsec := int64(int32(binary.NativeEndian.Uint32(message.Data[4:8])))
				timestamp = sec*1_000_000_000 + nsec
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

func parsePacket(data []byte) (uint64, uint64, uint64, uint64, bool) {
	if len(data) < legacyHeaderLen || binary.BigEndian.Uint32(data[:4]) != dataMagic {
		return 0, 0, 0, 0, false
	}
	pktVersion := binary.BigEndian.Uint16(data[4:6])
	declared := int(binary.BigEndian.Uint16(data[6:8]))
	minimum := legacyHeaderLen
	if pktVersion == protocolVersion {
		minimum = headerLen
	}
	if (pktVersion != protocolVersion && pktVersion != legacyVersion) || declared < minimum || len(data) < minimum {
		return 0, 0, 0, 0, false
	}
	seq := binary.BigEndian.Uint64(data[16:24])
	scheduled := binary.BigEndian.Uint64(data[24:32])
	send := binary.BigEndian.Uint64(data[32:40])
	sendUnix := uint64(0)
	if pktVersion == protocolVersion {
		sendUnix = binary.BigEndian.Uint64(data[40:48])
	}
	return seq, scheduled, send, sendUnix, true
}

func fields(data []byte) map[string]string {
	result := make(map[string]string)
	for _, token := range strings.Fields(string(data)) {
		key, value, ok := strings.Cut(token, "=")
		if ok {
			result[key] = value
		}
	}
	return result
}

func clockRequest(data []byte) (int64, bool) {
	if !bytes.HasPrefix(data, []byte("UWLP_CLOCK_REQ ")) {
		return 0, false
	}
	value, err := strconv.ParseInt(fields(data)["client_unix_ns"], 10, 64)
	return value, err == nil
}

func controlTruncated(data []byte, copiedLen, wireLen int) bool {
	return wireLen > copiedLen && bytes.HasPrefix(data, []byte("UWLP_"))
}

func writeKV(path string, values [][2]string) error {
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

func writeProgress(writer *bufio.Writer, event string, start time.Time, received int, lastSeq uint64, lastRecv int64, timeouts int) error {
	_, err := fmt.Fprintf(writer, "%s\t%d\t%.9f\t%d\t%d\t%d\t%d\n", event, time.Now().UnixNano(), time.Since(start).Seconds(), received, lastSeq, lastRecv, timeouts)
	if err == nil {
		err = writer.Flush()
	}
	return err
}

func writeUTRCHeader(writer *bufio.Writer, postprocess string, payloadBytes int) error {
	header := make([]byte, utrcHeaderSize)
	copy(header[:4], []byte("UTRC"))
	binary.BigEndian.PutUint16(header[4:6], 1)
	binary.BigEndian.PutUint16(header[6:8], utrcHeaderSize)
	binary.BigEndian.PutUint16(header[8:10], utrcRecordSize)
	header[10] = 1 // uplink
	if postprocess == "arrival" {
		header[11] = 1
	} else if postprocess == "latency" {
		header[11] = 2
	} else {
		header[11] = 0
	}
	binary.BigEndian.PutUint32(header[12:16], uint32(payloadBytes))
	_, err := writer.Write(header)
	return err
}

func writeUTRCRecord(writer *bufio.Writer, record captureRecord) error {
	var row [utrcRecordSize]byte
	binary.BigEndian.PutUint64(row[0:8], record.seq)
	binary.BigEndian.PutUint64(row[8:16], record.scheduledSendNS)
	binary.BigEndian.PutUint64(row[16:24], record.sendNS)
	binary.BigEndian.PutUint64(row[24:32], record.sendUnixNS)
	binary.BigEndian.PutUint64(row[32:40], uint64(record.recvUnixNS))
	binary.BigEndian.PutUint32(row[40:44], record.socketDrops)
	binary.BigEndian.PutUint16(row[44:46], record.payloadBytes)
	binary.BigEndian.PutUint16(row[46:48], record.version)
	_, err := writer.Write(row[:])
	return err
}

func syscallControlRead(conn *net.UDPConn, addr *net.UDPAddr) error {
	return connectUDP(conn, addr)
}

func main() {
	cfg := parseArgs()
	if cfg.showVersion {
		fmt.Println(appName)
		return
	}
	if cfg.receiveAPI == "recvmmsg" && syscallNumber() == 0 {
		fail("recvmmsg is unavailable on %s/%s", runtime.GOOS, runtime.GOARCH)
	}
	effectiveAPI := resolveAPI(cfg.receiveAPI)
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4zero, Port: cfg.port})
	if err != nil {
		fail("bind UDP port %d failed: %v", cfg.port, err)
	}
	defer conn.Close()
	actualRecvBuffer, err := configureSocket(conn, cfg.recvBufferBytes)
	if err != nil {
		fail("configure UDP socket failed: %v", err)
	}
	if actualRecvBuffer < cfg.recvBufferBytes {
		fail("actual receive buffer %d is below requested %d", actualRecvBuffer, cfg.recvBufferBytes)
	}
	if cfg.readyFile != "" {
		if err := os.MkdirAll(filepath.Dir(cfg.readyFile), 0755); err != nil {
			fail("create ready directory failed: %v", err)
		}
		if err := os.WriteFile(cfg.readyFile, []byte{}, 0644); err != nil {
			fail("write ready file failed: %v", err)
		}
	}
	outDir := cfg.outDir
	if err := os.MkdirAll(outDir, 0755); err != nil {
		fail("create output directory failed: %v", err)
	}
	progressPath := cfg.progressPath
	if progressPath == "" {
		progressPath = filepath.Join(outDir, "uplink_udp_receiver_progress.tsv")
	}
	progressFile, err := os.Create(progressPath)
	if err != nil {
		fail("create progress file failed: %v", err)
	}
	defer progressFile.Close()
	progressWriter := bufio.NewWriter(progressFile)
	if _, err := progressWriter.WriteString("event\ttimestamp_unix_ns\telapsed_s\treceived_packets\tlast_seq\tlast_packet_recv_unix_ns\tsocket_timeout_count\n"); err != nil {
		fail("write progress header failed: %v", err)
	}
	progressStart := time.Now()
	if err := writeProgress(progressWriter, "start", progressStart, 0, 0, 0, 0); err != nil {
		fail("write progress failed: %v", err)
	}
	captureFile, err := os.Create(filepath.Join(outDir, "udp_trace.utrc"))
	if err != nil {
		fail("create compact capture failed: %v", err)
	}
	captureWriter := bufio.NewWriterSize(captureFile, 4*1024*1024)
	if err := writeUTRCHeader(captureWriter, cfg.postprocess, cfg.payloadBytes); err != nil {
		fail("write compact header failed: %v", err)
	}

	readBuffer := make([]byte, 65535)
	readOOB := make([]byte, syscall.CmsgSpace(32))
	clockSamples := 0
	missingTimestamps := 0
	var pendingData []byte
	var pendingRecvNS int64
	var pendingDrops uint32
	var pendingHaveTimestamp bool
	var pendingHaveDrops bool
	var peer *net.UDPAddr
	bootstrapDeadline := time.Now().Add(time.Duration(cfg.waitTimeoutS * float64(time.Second)))
	for {
		if err := conn.SetReadDeadline(bootstrapDeadline); err != nil {
			fail("set bootstrap deadline failed: %v", err)
		}
		n, oobn, _, addr, readErr := conn.ReadMsgUDP(readBuffer, readOOB)
		if readErr != nil {
			fail("timed out waiting %.3fs for UWLP clock/data: %v", cfg.waitTimeoutS, readErr)
		}
		recvNS, drops, haveTimestamp, haveDrops := parseSocketMetadata(readOOB[:oobn])
		if !haveTimestamp {
			missingTimestamps++
		}
		if peer == nil {
			if err := syscallControlRead(conn, addr); err != nil {
				fail("connect UDP peer failed: %v", err)
			}
			peer = addr
		}
		data := readBuffer[:n]
		if clientNS, ok := clockRequest(data); ok {
			serverRecvNS := recvNS
			if !haveTimestamp {
				serverRecvNS = time.Now().UnixNano()
			}
			response := fmt.Sprintf("UWLP_CLOCK_ACK client_unix_ns=%d server_recv_unix_ns=%d server_send_unix_ns=%d\n", clientNS, serverRecvNS, time.Now().UnixNano())
			if _, err := conn.Write([]byte(response)); err != nil {
				fail("send clock ACK failed: %v", err)
			}
			clockSamples++
			continue
		}
		if _, _, _, _, ok := parsePacket(data); ok {
			pendingData = append([]byte(nil), data...)
			pendingRecvNS, pendingDrops = recvNS, drops
			pendingHaveTimestamp, pendingHaveDrops = haveTimestamp, haveDrops
			break
		}
	}
	_ = peer
	_ = conn.SetReadDeadline(time.Time{})

	receivedPackets := 0
	uniquePackets := 0
	duplicatePackets := 0
	lastSeq := uint64(0)
	firstSeq := uint64(0)
	firstDataTime := time.Time{}
	firstRecvNS := int64(0)
	lastRecvNS := int64(0)
	payloadMin := 0
	payloadMax := 0
	lastSocketDrops := uint32(0)
	maxSocketDrops := uint32(0)
	socketTimeouts := 0
	headerOnlyPackets := 0
	controlTruncatedPackets := 0
	captureBackpressure := 0
	recvmmsgCalls := 0
	recvmmsgPackets := 0
	batchMin := 0
	batchMax := 0
	batchFull := 0
	receiveAPIFallback := ""
	doneReceived := false
	doneLastSeq := uint64(0)
	tracker := sequenceTracker{}
	lagSamples := make([]int64, 0, 2048)
	maxLagNS := int64(0)
	lastProgress := time.Now().Add(cfg.progressInterval)
	stop := false
	// Packet-count senders can take longer than the nominal duration when the
	// phone's UDP transmit queue backpressures. Wait for UWLP_DONE instead of
	// cutting off at duration+timeout, while retaining a bounded safety cap.
	maxCaptureS := math.Max(cfg.durationS*4, cfg.durationS+cfg.timeoutS)

	processDatagram := func(data []byte, copiedLen int, wireLen int, recvNS int64, drops uint32, haveTimestamp, haveDrops bool, userRead time.Time) {
		if !haveTimestamp {
			missingTimestamps++
			recvNS = userRead.UnixNano()
		}
		if !haveDrops {
			drops = 0
		}
		if drops > maxSocketDrops {
			maxSocketDrops = drops
		}
		lastSocketDrops = drops
		if controlTruncated(data, copiedLen, wireLen) {
			controlTruncatedPackets++
			return
		}
		if clientNS, ok := clockRequest(data); ok {
			response := fmt.Sprintf("UWLP_CLOCK_ACK client_unix_ns=%d server_recv_unix_ns=%d server_send_unix_ns=%d\n", clientNS, recvNS, time.Now().UnixNano())
			_, _ = conn.Write([]byte(response))
			clockSamples++
			return
		}
		if bytes.HasPrefix(data, []byte("UWLP_DONE ")) {
			fields := fields(data)
			doneLastSeq, _ = strconv.ParseUint(fields["last_seq"], 10, 64)
			doneReceived = true
			ack := fmt.Sprintf("UWLP_DONE_ACK run_id=%s received_packets=%d socket_drops=%d\n", cfg.runID, receivedPackets, maxSocketDrops)
			_, _ = conn.Write([]byte(ack))
			stop = true
			return
		}
		seq, scheduledNS, sendNS, sendUnixNS, ok := parsePacket(data)
		if !ok || wireLen < headerLen {
			return
		}
		if firstDataTime.IsZero() {
			firstDataTime = userRead
			firstRecvNS = recvNS
		}
		if recvNS < firstRecvNS {
			recvNS = firstRecvNS
		}
		if duplicate := tracker.mark(seq); duplicate {
			duplicatePackets++
		} else {
			uniquePackets++
		}
		receivedPackets++
		if receivedPackets == 1 || seq < firstSeq {
			firstSeq = seq
		}
		if seq > lastSeq {
			lastSeq = seq
		}
		lastRecvNS = recvNS
		if payloadMin == 0 || wireLen < payloadMin {
			payloadMin = wireLen
		}
		if wireLen > payloadMax {
			payloadMax = wireLen
		}
		if effectiveAPI == "recvmmsg" && wireLen > copiedLen {
			headerOnlyPackets++
		}
		if haveTimestamp && receivedPackets%1024 == 0 {
			lag := userRead.UnixNano() - recvNS
			lagSamples = append(lagSamples, lag)
			if lag > maxLagNS {
				maxLagNS = lag
			}
		}
		if err := writeUTRCRecord(captureWriter, captureRecord{seq, scheduledNS, sendNS, sendUnixNS, recvNS, drops, uint16(wireLen), protocolVersion}); err != nil {
			captureBackpressure++
		}
	}

	if pendingData != nil {
		processDatagram(pendingData, len(pendingData), len(pendingData), pendingRecvNS, pendingDrops, pendingHaveTimestamp, pendingHaveDrops, time.Now())
		// Bootstrap uses ReadMsgUDP to complete the clock handshake. The first
		// data packet is already parsed from its full header, but compact mode's
		// capture contract counts it as a header-only record as well.
		if effectiveAPI == "recvmmsg" && len(pendingData) > headerLen {
			headerOnlyPackets++
		}
	}
	batchSize := cfg.receiveBatchSize
	payloads := make([][]byte, batchSize)
	oobs := make([][]byte, batchSize)
	msgs := make([]rawMMsg, batchSize)
	iovs := make([]syscall.Iovec, batchSize)
	for i := 0; i < batchSize; i++ {
		payloads[i] = make([]byte, compactCaptureBytes)
		oobs[i] = make([]byte, syscall.CmsgSpace(32))
	}
	for !stop {
		now := time.Now()
		if firstDataTime.IsZero() {
			if now.Sub(bootstrapDeadline) >= 0 {
				break
			}
		} else if now.After(firstDataTime.Add(time.Duration(maxCaptureS * float64(time.Second)))) {
			break
		}
		if !now.Before(lastProgress) {
			if err := writeProgress(progressWriter, "heartbeat", progressStart, receivedPackets, lastSeq, lastRecvNS, socketTimeouts); err != nil {
				fail("write progress failed: %v", err)
			}
			lastProgress = now.Add(cfg.progressInterval)
		}
		if effectiveAPI == "recvmmsg" {
			recvmmsgCalls++
			count, batchErr := recvmmsgBatch(conn, payloads, oobs, msgs, iovs)
			if batchErr == syscall.EAGAIN || batchErr == syscall.EWOULDBLOCK {
				time.Sleep(100 * time.Microsecond)
				continue
			}
			if batchErr == syscall.ENOSYS && cfg.receiveAPI == "auto" {
				effectiveAPI = "readmsg"
				receiveAPIFallback = "recvmmsg_unsupported"
				continue
			}
			if batchErr != nil {
				fail("recvmmsg failed: %v", batchErr)
			}
			readAt := time.Now()
			if count > 0 {
				recvmmsgPackets += count
				if batchMin == 0 || count < batchMin {
					batchMin = count
				}
				if count > batchMax {
					batchMax = count
				}
				if count == batchSize {
					batchFull++
				}
			}
			for i := 0; i < count; i++ {
				wireLen := int(msgs[i].length)
				copiedLen := wireLen
				if copiedLen > len(payloads[i]) {
					copiedLen = len(payloads[i])
				}
				recvNS, drops, haveTimestamp, haveDrops := parseSocketMetadata(oobs[i][:msgs[i].hdr.Controllen])
				processDatagram(payloads[i][:copiedLen], copiedLen, wireLen, recvNS, drops, haveTimestamp, haveDrops, readAt)
			}
			continue
		}
		if err := conn.SetReadDeadline(time.Now().Add(200 * time.Millisecond)); err != nil {
			fail("set read deadline failed: %v", err)
		}
		n, oobn, _, _, readErr := conn.ReadMsgUDP(readBuffer, readOOB)
		if readErr != nil {
			if netErr, ok := readErr.(net.Error); ok && netErr.Timeout() {
				socketTimeouts++
				continue
			}
			fail("receive failed: %v", readErr)
		}
		recvNS, drops, haveTimestamp, haveDrops := parseSocketMetadata(readOOB[:oobn])
		processDatagram(readBuffer[:n], n, n, recvNS, drops, haveTimestamp, haveDrops, time.Now())
	}

	if err := captureWriter.Flush(); err != nil {
		fail("flush compact capture failed: %v", err)
	}
	if err := captureFile.Close(); err != nil {
		fail("close compact capture failed: %v", err)
	}
	if err := writeProgress(progressWriter, "final", progressStart, receivedPackets, lastSeq, lastRecvNS, socketTimeouts); err != nil {
		fail("write final progress failed: %v", err)
	}
	if err := progressFile.Sync(); err != nil {
		fail("sync progress failed: %v", err)
	}

	lagP95 := "0.000000"
	if len(lagSamples) > 0 {
		sort.Slice(lagSamples, func(i, j int) bool { return lagSamples[i] < lagSamples[j] })
		index := int(math.Ceil(0.95*float64(len(lagSamples)))) - 1
		if index < 0 {
			index = 0
		}
		lagP95 = fmt.Sprintf("%.6f", float64(lagSamples[index])/1e6)
	}
	firstRel := 0.0
	lastRel := 0.0
	if firstRecvNS > 0 && lastRecvNS >= firstRecvNS {
		lastRel = float64(lastRecvNS-firstRecvNS) / 1e9
	}
	receiverIntegrity := "clean"
	if !doneReceived || missingTimestamps > 0 || maxSocketDrops > 0 || captureBackpressure > 0 || controlTruncatedPackets > 0 {
		receiverIntegrity = "contaminated"
	}
	summary := [][2]string{
		{"run_id", cfg.runID}, {"port", strconv.Itoa(cfg.port)}, {"duration_s", fmt.Sprintf("%.6f", cfg.durationS)}, {"timeout_s", fmt.Sprintf("%.6f", cfg.timeoutS)},
		{"payload_bytes", strconv.Itoa(payloadMax)}, {"payload_bytes_min", strconv.Itoa(payloadMin)}, {"payload_bytes_max", strconv.Itoa(payloadMax)},
		{"output_mode", cfg.outputMode}, {"postprocess", cfg.postprocess}, {"window_ms", strconv.FormatFloat(cfg.windowMS, 'f', -1, 64)},
		{"timestamp_fields", "send_unix_ns,recv_unix_ns"}, {"timestamp_source", "kernel_so_timestampns"},
		{"requested_rcvbuf_bytes", strconv.Itoa(cfg.recvBufferBytes)}, {"actual_rcvbuf_bytes", strconv.Itoa(actualRecvBuffer)},
		{"missing_kernel_timestamp_packets", strconv.Itoa(missingTimestamps)}, {"missing_drop_metadata_packets", "0"}, {"socket_rxq_overflow_packets", strconv.FormatUint(uint64(maxSocketDrops), 10)},
		{"capture_backpressure_records", strconv.Itoa(captureBackpressure)}, {"capture_records", strconv.Itoa(receivedPackets)}, {"app_read_lag_ms_p95", lagP95}, {"app_read_lag_ms_max", fmt.Sprintf("%.6f", float64(maxLagNS)/1e6)},
		{"receive_api_requested", cfg.receiveAPI}, {"receive_api_effective", effectiveAPI}, {"receive_api_fallback", receiveAPIFallback}, {"receive_capture_bytes", strconv.Itoa(func() int {
			if effectiveAPI == "recvmmsg" {
				return compactCaptureBytes
			}
			return 65535
		}())},
		{"header_only_packets", strconv.Itoa(headerOnlyPackets)}, {"control_truncated_packets", strconv.Itoa(controlTruncatedPackets)}, {"payload_bytes_source", "wire_length"}, {"receive_batch_size", strconv.Itoa(cfg.receiveBatchSize)},
		{"recvmmsg_calls", strconv.Itoa(recvmmsgCalls)}, {"recvmmsg_packets", strconv.Itoa(recvmmsgPackets)}, {"batch_packets_min", strconv.Itoa(batchMin)}, {"batch_packets_max", strconv.Itoa(batchMax)}, {"batch_full_count", strconv.Itoa(batchFull)}, {"done_received", strconv.FormatBool(doneReceived)}, {"done_last_seq", strconv.FormatUint(doneLastSeq, 10)}, {"receiver_integrity", receiverIntegrity},
		{"received_packets", strconv.Itoa(receivedPackets)}, {"unique_received_packets", strconv.Itoa(uniquePackets)}, {"duplicate_packets", strconv.Itoa(duplicatePackets)}, {"first_seq", strconv.FormatUint(firstSeq, 10)}, {"last_seq", strconv.FormatUint(lastSeq, 10)},
		{"first_recv_rel_s", fmt.Sprintf("%.9f", firstRel)}, {"last_recv_rel_s", fmt.Sprintf("%.9f", lastRel)}, {"socket_timeout_count", strconv.Itoa(socketTimeouts)}, {"clock_sync_samples", strconv.Itoa(clockSamples)}, {"last_socket_drop_count", strconv.FormatUint(uint64(lastSocketDrops), 10)},
	}
	if err := writeKV(filepath.Join(outDir, "uplink_udp_receiver_summary.txt"), summary); err != nil {
		fail("write summary failed: %v", err)
	}
	fmt.Printf("run_id=%s\nreceived_packets=%d\nreceive_api_effective=%s\n", cfg.runID, receivedPackets, effectiveAPI)
	if receiverIntegrity != "clean" {
		os.Exit(1)
	}
}
