//go:build udpecho

package main

// Header-only UDP echo receiver. It deliberately copies only the probe header
// into userspace while MSG_TRUNC preserves the wire length for accounting.
import (
	"encoding/binary"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"
)

const headerLen = 32
const magic uint32 = 0x46525454

func main() {
	port := flag.Int("port", 5001, "UDP port")
	ready := flag.String("ready-file", "", "ready marker")
	out := flag.String("out-dir", ".", "output directory")
	runID := flag.String("run-id", "", "run id")
	receiveAPI := flag.String("receive-api", "readmsg", "receive API (readmsg; recvmmsg is reserved for the shared receiver)")
	flag.Parse()
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4zero, Port: *port})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer conn.Close()
	raw, err := conn.SyscallConn()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	_ = raw.Control(func(fd uintptr) {
		_ = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF, 64*1024*1024)
		_ = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, 40, 1) // SO_RXQ_OVFL
		_ = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, 35, 1) // SO_TIMESTAMPNS
	})
	if *ready != "" {
		if err := os.WriteFile(*ready, nil, 0644); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
	}
	if err := os.MkdirAll(*out, 0755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	f, err := os.Create(*out + "/udp_echo_receiver_summary.txt")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer f.Close()
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(stop)
	buf := make([]byte, 256)
	oob := make([]byte, 128)
	echoed, invalid, truncated, overflow := 0, 0, 0, 0
	valid, wirePackets, wireBytes := 0, 0, 0
	for {
		select {
		case <-stop:
			goto done
		default:
		}
		_ = conn.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
		n, flags, oobn, addr, err := conn.ReadMsgUDP(buf, oob)
		if err != nil {
			if e, ok := err.(net.Error); ok && e.Timeout() {
				continue
			}
			break
		}
		if flags&syscall.MSG_TRUNC != 0 {
			truncated++
		}
		wirePackets++
		wireBytes += n
		if drops, ok := socketOverflow(oob[:oobn]); ok && int(drops) > overflow {
			overflow = int(drops)
		}
		if n < headerLen || binary.BigEndian.Uint32(buf[:4]) != magic || binary.BigEndian.Uint16(buf[4:6]) != 1 || int(binary.BigEndian.Uint16(buf[6:8])) > n {
			invalid++
			continue
		}
		valid++
		// Echo only the fixed header; payload bytes are intentionally ignored.
		if _, err := conn.WriteToUDP(buf[:headerLen], addr); err == nil {
			echoed++
		}
	}
done:
	fmt.Fprintf(f, "run_id=%s\nport=%d\nechoed_packets=%d\nvalid_packets=%d\ninvalid_packets=%d\nwire_packets=%d\nwire_bytes=%d\ntruncated_packets=%d\nsocket_rxq_overflow_packets=%d\nreceive_api=%s\necho_header_bytes=%d\nheader_only=true\n", *runID, *port, echoed, valid, invalid, wirePackets, wireBytes, truncated, overflow, *receiveAPI, headerLen)
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
