# Phone packet ledger v1

`pcap_packet_ledger.go` reads a closed classic pcap, including every record
in capture order. It hashes the complete source pcap and preserves integer
analysis fields. It does not preserve packet payload or uncaptured bytes.
Extraction is permitted only after the workload and both capture processes
have exited. The binary is installed before measurement and uses only Go's
standard library.

The uncompressed stream contains one 32-byte header followed by 96-byte
records. All integers use little endian. Independent gzip chunks contain at
most 32 MiB of uncompressed data and end at record boundaries. Only the first
chunk contains a header. `ledger_manifest.json` contains compressed chunk
sizes/hashes, uncompressed sizes, the full uncompressed stream SHA-256,
source pcap size/hash, classification counts, and final tcpdump counters.

## Header

| Offset | Bytes | Field |
| --- | --- | --- |
| 0 | 8 | ASCII `STLGR001` |
| 8 | 2 | Version = 1 |
| 10 | 2 | Record size = 96 |
| 12 | 4 | PCAP link type |
| 16 | 4 | PCAP snaplen |
| 20 | 1 | PCAP resolution: 0 = microseconds, 1 = nanoseconds |
| 21 | 1 | Port direction: 0 = destination, 1 = source |
| 22 | 2 | Selected UDP port |
| 24 | 8 | Reserved zeros |

## Record

| Offset | Bytes | Field |
| --- | --- | --- |
| 0 | 8 | Record index, starting at 0 |
| 8 | 8 | Exact capture timestamp in integer nanoseconds |
| 16 | 4 | Captured frame length |
| 20 | 4 | Original frame length |
| 24 | 1 | IP version: 0 = unavailable, 4 or 6 |
| 25 | 1 | Status flags |
| 26 | 1 | iperf decode status |
| 27 | 1 | Reserved zero |
| 28 | 16 | Source IP bytes; IPv4 uses first 4 bytes |
| 44 | 16 | Destination IP bytes; IPv4 uses first 4 bytes |
| 60 | 2 | Source port |
| 62 | 2 | Destination port |
| 64 | 4 | Declared UDP payload length |
| 68 | 4 | Captured UDP payload length |
| 72 | 8 | Signed iperf sequence, -1 if unavailable |
| 80 | 8 | Integer iperf send timestamp in nanoseconds |
| 88 | 2 | Raw UDP length field |
| 90 | 6 | Reserved zeros |

Status bits are target UDP (1), filtered (2), parse failure (4), truncated (8),
and decoded iperf (16). Fields unavailable because of parsing failure are
zero; the record index, timestamp and lengths are always preserved.
Decode statuses are disabled (0), decoded (1), short captured header (2),
invalid microseconds (3), and negative sequence (4). Root v3 iperf workloads
explicitly negotiate 64-bit counters: seconds and microseconds occupy the
first 8 payload bytes, followed by the 64-bit sequence. Failed decoding does
not imply packet loss. See the [iperf3 UDP implementation](https://github.com/esnet/iperf/blob/master/src/iperf_udp.c).

Incomplete pcap headers/data or a record count different from tcpdump's
`packets captured` abort extraction without deleting the source. The local
reader checks each chunk's gzip CRC, sizes and SHA-256, the full ledger hash,
record indexes, counts and tcpdump output before publishing derived data.
