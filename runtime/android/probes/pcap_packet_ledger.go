// pcap_packet_ledger extracts analysis fields only after tcpdump has exited.
// The output is a sequence of independently compressed, checksummed chunks.
package main

import (
	"compress/gzip"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"hash"
	"io"
	"os"
	"path/filepath"
)

const chunkLimit = 32 * 1024 * 1024
const recordSize = 96
const headerSize = 32

const (
	flagTarget = 1 << iota
	flagFiltered
	flagParseError
	flagTruncated
	flagIperfDecoded
)

type chunkInfo struct {
	Name              string `json:"name"`
	SizeBytes         int64  `json:"size_bytes"`
	UncompressedBytes int64  `json:"uncompressed_bytes"`
	SHA256            string `json:"sha256"`
}

type manifest struct {
	SchemaVersion int              `json:"schema_version"`
	Format        string           `json:"format"`
	RecordSize    int              `json:"record_size"`
	HeaderSize    int              `json:"header_size"`
	Linktype      uint32           `json:"linktype"`
	Snaplen       uint32           `json:"snaplen"`
	Resolution    string           `json:"timestamp_resolution"`
	Port          int              `json:"port"`
	Direction     string           `json:"direction"`
	DecodeIperf   bool             `json:"decode_iperf"`
	PCAPSize      int64            `json:"pcap_size_bytes"`
	PCAPSHA256    string           `json:"pcap_sha256"`
	LedgerBytes   int64            `json:"ledger_bytes"`
	LedgerSHA256  string           `json:"ledger_sha256"`
	RecordsSeen   uint64           `json:"records_seen"`
	Target        uint64           `json:"target_packets"`
	Filtered      uint64           `json:"filtered_records"`
	ParseErrors   uint64           `json:"parse_errors"`
	Truncated     uint64           `json:"truncated_records"`
	IperfDecoded  uint64           `json:"iperf_decoded_records"`
	TCPDump       map[string]int64 `json:"tcpdump"`
	Chunks        []chunkInfo      `json:"chunks"`
}

type chunkWriter struct {
	dir    string
	file   *os.File
	gzip   *gzip.Writer
	hash   hash.Hash
	whole  hash.Hash
	bytes  int64
	total  int64
	chunks []chunkInfo
}

func (w *chunkWriter) open() error {
	name := fmt.Sprintf("chunk-%06d.gz", len(w.chunks))
	f, err := os.Create(filepath.Join(w.dir, name))
	if err != nil {
		return err
	}
	w.file, w.hash, w.bytes = f, sha256.New(), 0
	w.gzip, err = gzip.NewWriterLevel(io.MultiWriter(f, w.hash), gzip.DefaultCompression)
	if err != nil {
		_ = f.Close()
		return err
	}
	return nil
}

func (w *chunkWriter) close() error {
	if w.file == nil {
		return nil
	}
	if err := w.gzip.Close(); err != nil {
		_ = w.file.Close()
		return err
	}
	if err := w.file.Sync(); err != nil {
		_ = w.file.Close()
		return err
	}
	info, err := w.file.Stat()
	if err != nil {
		_ = w.file.Close()
		return err
	}
	if err := w.file.Close(); err != nil {
		return err
	}
	w.chunks = append(w.chunks, chunkInfo{
		Name: fmt.Sprintf("chunk-%06d.gz", len(w.chunks)), SizeBytes: info.Size(),
		UncompressedBytes: w.bytes, SHA256: hex.EncodeToString(w.hash.Sum(nil)),
	})
	w.file = nil
	return nil
}

func (w *chunkWriter) write(data []byte) error {
	if w.file == nil {
		if err := w.open(); err != nil {
			return err
		}
	}
	if w.bytes > 0 && w.bytes+int64(len(data)) > chunkLimit {
		if err := w.close(); err != nil {
			return err
		}
		if err := w.open(); err != nil {
			return err
		}
	}
	n, err := w.gzip.Write(data)
	if err != nil {
		return err
	}
	if n != len(data) {
		return io.ErrShortWrite
	}
	_, _ = w.whole.Write(data)
	w.bytes += int64(n)
	w.total += int64(n)
	return nil
}

type parsed struct {
	version                     byte
	status                      byte
	srcIP, dstIP                [16]byte
	srcPort, dstPort            uint16
	udpLength                   uint16
	payloadLen, capturedPayload uint32
	seq                         int64
	sendNS                      uint64
	decodeStatus                byte
}

func parseFrame(frame []byte, linktype uint32, port uint16, direction string, decodeIperf bool) parsed {
	p := parsed{status: flagFiltered, seq: -1}
	offset, ether := 0, uint16(0)
	switch linktype {
	case 1:
		if len(frame) < 14 {
			p.status = flagParseError
			return p
		}
		offset, ether = 14, binary.BigEndian.Uint16(frame[12:14])
		for ether == 0x8100 || ether == 0x88a8 || ether == 0x9100 {
			if len(frame) < offset+4 {
				p.status = flagParseError
				return p
			}
			ether = binary.BigEndian.Uint16(frame[offset+2 : offset+4])
			offset += 4
		}
	case 113:
		if len(frame) < 16 {
			p.status = flagParseError
			return p
		}
		offset, ether = 16, binary.BigEndian.Uint16(frame[14:16])
	case 276:
		if len(frame) < 20 {
			p.status = flagParseError
			return p
		}
		offset, ether = 20, binary.BigEndian.Uint16(frame[0:2])
	case 101, 228, 229:
		if len(frame) == 0 {
			p.status = flagParseError
			return p
		}
		if frame[0]>>4 == 4 {
			ether = 0x0800
		} else if frame[0]>>4 == 6 {
			ether = 0x86dd
		}
	case 0, 108:
		if len(frame) < 5 {
			p.status = flagParseError
			return p
		}
		offset = 4
		if frame[4]>>4 == 4 {
			ether = 0x0800
		} else if frame[4]>>4 == 6 {
			ether = 0x86dd
		}
	default:
		p.status = flagParseError
		return p
	}
	udp := 0
	switch ether {
	case 0x0800:
		if len(frame) < offset+20 || frame[offset]>>4 != 4 {
			p.status = flagParseError
			return p
		}
		hlen := int(frame[offset]&15) * 4
		if hlen < 20 || len(frame) < offset+hlen {
			p.status = flagParseError
			return p
		}
		p.version = 4
		copy(p.srcIP[:4], frame[offset+12:offset+16])
		copy(p.dstIP[:4], frame[offset+16:offset+20])
		if frame[offset+9] != 17 {
			return p
		}
		udp = offset + hlen
	case 0x86dd:
		if len(frame) < offset+40 || frame[offset]>>4 != 6 {
			p.status = flagParseError
			return p
		}
		p.version = 6
		copy(p.srcIP[:], frame[offset+8:offset+24])
		copy(p.dstIP[:], frame[offset+24:offset+40])
		next := frame[offset+6]
		udp = offset + 40
		for next == 0 || next == 43 || next == 60 || next == 44 || next == 51 {
			if len(frame) < udp+2 {
				p.status = flagParseError
				return p
			}
			length := (int(frame[udp+1]) + 1) * 8
			if next == 44 {
				length = 8
			} else if next == 51 {
				length = (int(frame[udp+1]) + 2) * 4
			}
			if len(frame) < udp+length {
				p.status = flagParseError
				return p
			}
			next = frame[udp]
			udp += length
		}
		if next != 17 {
			return p
		}
	default:
		return p
	}
	if len(frame) < udp+8 {
		p.status = flagParseError
		return p
	}
	p.srcPort = binary.BigEndian.Uint16(frame[udp : udp+2])
	p.dstPort = binary.BigEndian.Uint16(frame[udp+2 : udp+4])
	length := binary.BigEndian.Uint16(frame[udp+4 : udp+6])
	p.udpLength = length
	if length < 8 {
		p.status = flagParseError
		return p
	}
	p.payloadLen = uint32(length - 8)
	p.capturedPayload = uint32(len(frame) - udp - 8)
	if direction == "source" && p.srcPort != port || direction == "destination" && p.dstPort != port {
		return p
	}
	p.status = flagTarget
	if decodeIperf {
		p.decodeStatus = 2 // captured UDP header is too short
	}
	if decodeIperf && len(frame) >= udp+24 {
		payload := frame[udp+8:]
		seq := int64(binary.BigEndian.Uint64(payload[8:16]))
		micros := binary.BigEndian.Uint32(payload[4:8])
		if micros >= 1_000_000 {
			p.decodeStatus = 3
		} else if seq < 0 {
			p.decodeStatus = 4
		} else {
			p.seq = seq
			p.sendNS = uint64(binary.BigEndian.Uint32(payload[0:4]))*1_000_000_000 + uint64(micros)*1000
			p.status |= flagIperfDecoded
			p.decodeStatus = 1
		}
	}
	return p
}

func run() error {
	input := flag.String("input", "", "closed classic pcap file")
	out := flag.String("out-dir", "", "output directory")
	port := flag.Int("port", 0, "UDP port")
	direction := flag.String("direction", "destination", "source or destination")
	decodeIperf := flag.Bool("decode-iperf", false, "decode the 16-byte iperf3 UDP header")
	expected := flag.Int64("tcpdump-captured", -1, "tcpdump packets captured")
	received := flag.Int64("tcpdump-received-by-filter", -1, "tcpdump packets received by filter")
	dropped := flag.Int64("tcpdump-dropped-by-kernel", -1, "tcpdump packets dropped by kernel")
	flag.Parse()
	if *input == "" || *out == "" || *port < 1 || *port > 65535 || (*direction != "source" && *direction != "destination") {
		return errors.New("invalid input, output, port, or direction")
	}
	if *expected < 0 || *received < 0 || *dropped < 0 {
		return errors.New("all final tcpdump counters are required")
	}
	f, err := os.Open(*input)
	if err != nil {
		return err
	}
	defer f.Close()
	stat, err := f.Stat()
	if err != nil {
		return err
	}
	tmp := *out + ".tmp"
	if err := os.RemoveAll(tmp); err != nil {
		return err
	}
	if err := os.MkdirAll(tmp, 0755); err != nil {
		return err
	}
	sourceHash := sha256.New()
	reader := io.TeeReader(f, sourceHash)
	global := make([]byte, 24)
	if _, err := io.ReadFull(reader, global); err != nil {
		return fmt.Errorf("pcap global header: %w", err)
	}
	var order binary.ByteOrder = binary.LittleEndian
	resolution := "microsecond"
	switch string(global[:4]) {
	case "\xd4\xc3\xb2\xa1":
	case "\xa1\xb2\xc3\xd4":
		order = binary.BigEndian
	case "\x4d\x3c\xb2\xa1":
		resolution = "nanosecond"
	case "\xa1\xb2\x3c\x4d":
		order = binary.BigEndian
		resolution = "nanosecond"
	default:
		return errors.New("unsupported pcap magic")
	}
	if order.Uint16(global[4:6]) != 2 {
		return errors.New("unsupported pcap version")
	}
	snaplen, linktype := order.Uint32(global[16:20]), order.Uint32(global[20:24])
	if snaplen == 0 || snaplen > 1<<20 {
		return errors.New("invalid pcap snaplen")
	}
	m := manifest{SchemaVersion: 1, Format: "starlive-packet-ledger", RecordSize: recordSize, HeaderSize: headerSize,
		Linktype: linktype, Snaplen: snaplen, Resolution: resolution, Port: *port, Direction: *direction, DecodeIperf: *decodeIperf,
		PCAPSize: stat.Size(), TCPDump: map[string]int64{"packets_captured": *expected, "packets_received_by_filter": *received, "packets_dropped_by_kernel": *dropped}}
	w := chunkWriter{dir: tmp, whole: sha256.New()}
	header := make([]byte, headerSize)
	copy(header[:8], []byte("STLGR001"))
	binary.LittleEndian.PutUint16(header[8:10], 1)
	binary.LittleEndian.PutUint16(header[10:12], recordSize)
	binary.LittleEndian.PutUint32(header[12:16], linktype)
	binary.LittleEndian.PutUint32(header[16:20], snaplen)
	if resolution == "nanosecond" {
		header[20] = 1
	}
	if *direction == "source" {
		header[21] = 1
	}
	binary.LittleEndian.PutUint16(header[22:24], uint16(*port))
	if err := w.write(header); err != nil {
		return err
	}
	packetHead := make([]byte, 16)
	for {
		n, err := io.ReadFull(reader, packetHead)
		if err == io.EOF && n == 0 {
			break
		}
		if err != nil {
			return fmt.Errorf("truncated pcap packet header at record %d: %w", m.RecordsSeen, err)
		}
		sec, frac := order.Uint32(packetHead[:4]), order.Uint32(packetHead[4:8])
		caplen, origlen := order.Uint32(packetHead[8:12]), order.Uint32(packetHead[12:16])
		if caplen > snaplen {
			return fmt.Errorf("pcap record %d exceeds snaplen", m.RecordsSeen)
		}
		if resolution == "nanosecond" && frac >= 1_000_000_000 || resolution == "microsecond" && frac >= 1_000_000 {
			return fmt.Errorf("invalid timestamp fraction at record %d", m.RecordsSeen)
		}
		frame := make([]byte, caplen)
		if _, err := io.ReadFull(reader, frame); err != nil {
			return fmt.Errorf("truncated pcap packet %d: %w", m.RecordsSeen, err)
		}
		p := parseFrame(frame, linktype, uint16(*port), *direction, *decodeIperf)
		if caplen < origlen || p.capturedPayload < p.payloadLen && p.status&flagTarget != 0 {
			p.status |= flagTruncated
			m.Truncated++
		}
		if p.status&flagTarget != 0 {
			m.Target++
		} else if p.status&flagParseError != 0 {
			m.ParseErrors++
		} else {
			m.Filtered++
		}
		if p.status&flagIperfDecoded != 0 {
			m.IperfDecoded++
		}
		ts := uint64(sec)*1_000_000_000 + uint64(frac)
		if resolution == "microsecond" {
			ts = uint64(sec)*1_000_000_000 + uint64(frac)*1000
		}
		record := make([]byte, recordSize)
		binary.LittleEndian.PutUint64(record[0:8], m.RecordsSeen)
		binary.LittleEndian.PutUint64(record[8:16], ts)
		binary.LittleEndian.PutUint32(record[16:20], caplen)
		binary.LittleEndian.PutUint32(record[20:24], origlen)
		record[24], record[25] = p.version, p.status
		record[26] = p.decodeStatus
		copy(record[28:44], p.srcIP[:])
		copy(record[44:60], p.dstIP[:])
		binary.LittleEndian.PutUint16(record[60:62], p.srcPort)
		binary.LittleEndian.PutUint16(record[62:64], p.dstPort)
		binary.LittleEndian.PutUint32(record[64:68], p.payloadLen)
		binary.LittleEndian.PutUint32(record[68:72], p.capturedPayload)
		binary.LittleEndian.PutUint64(record[72:80], uint64(p.seq))
		binary.LittleEndian.PutUint64(record[80:88], p.sendNS)
		binary.LittleEndian.PutUint16(record[88:90], p.udpLength)
		if err := w.write(record); err != nil {
			return err
		}
		m.RecordsSeen++
	}
	if *expected >= 0 && uint64(*expected) != m.RecordsSeen {
		return fmt.Errorf("pcap records %d != tcpdump captured %d", m.RecordsSeen, *expected)
	}
	if err := w.close(); err != nil {
		return err
	}
	m.Chunks = w.chunks
	m.LedgerBytes = w.total
	m.LedgerSHA256 = hex.EncodeToString(w.whole.Sum(nil))
	m.PCAPSHA256 = hex.EncodeToString(sourceHash.Sum(nil))
	encoded, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(tmp, "ledger_manifest.json"), append(encoded, '\n'), 0644); err != nil {
		return err
	}
	if _, err := os.Stat(*out); err == nil {
		return errors.New("output already exists; use existing manifest for recovery")
	} else if !os.IsNotExist(err) {
		return err
	}
	return os.Rename(tmp, *out)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "ERROR:", err)
		os.Exit(1)
	}
}
