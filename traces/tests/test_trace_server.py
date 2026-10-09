from __future__ import annotations

import json
from argparse import Namespace
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runtime.server.trace_server import process_pcap_on_server, run_udp_echo


ROOT = Path(__file__).resolve().parents[2]


def write_pcap(path: Path, *, source_port: int = 40000, destination_port: int = 5201) -> None:
    ethernet = bytes.fromhex("00112233445566778899aabb0800")
    ip_header = bytearray(20)
    ip_header[0] = 0x45
    struct.pack_into(">H", ip_header, 2, 20 + 8 + 100)
    ip_header[8] = 64
    ip_header[9] = 17
    udp_header = struct.pack(">HHHH", source_port, destination_port, 108, 0)
    frame = ethernet + bytes(ip_header) + udp_header + bytes(100)
    with path.open("wb") as handle:
        handle.write(b"\xd4\xc3\xb2\xa1")
        handle.write(struct.pack("<HHIIII", 2, 4, 0, 0, 2048, 1))
        handle.write(struct.pack("<IIII", 1, 0, len(frame), len(frame)))
        handle.write(frame)


class TraceServerPcapTest(unittest.TestCase):
    def test_echo_capture_exits_before_pcap_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            receiver = run_dir / "receiver"
            receiver.touch()
            order = []
            tcpdump = mock.Mock()
            tcpdump.poll.return_value = None
            tcpdump.send_signal.side_effect = lambda _signal: order.append("capture_stopped")
            tcpdump.wait.side_effect = lambda **_kwargs: order.append("capture_exited") or 0
            child = mock.Mock()
            child.poll.return_value = 0
            child.returncode = 0
            service = mock.Mock()
            service.start.side_effect = [tcpdump, child]
            args = Namespace(receiver_bin=str(receiver), run_dir=run_dir, port=5201, run_id="test", ready_file=run_dir / "ready",
                             tcpdump=True, interface="any", snaplen=256, pcap_processor=run_dir / "processor")
            with mock.patch("runtime.server.trace_server.Service", return_value=service), \
                 mock.patch("runtime.server.trace_server.command_exists", return_value=True), \
                 mock.patch("runtime.server.trace_server.time.sleep"), \
                 mock.patch("runtime.server.trace_server.process_pcap_on_server", side_effect=lambda *_args: order.append("postprocess") or 0):
                self.assertEqual(run_udp_echo(args), 0)
            self.assertEqual(order, ["capture_stopped", "capture_exited", "postprocess"])

    def test_server_side_pcap_processor_writes_small_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            pcap = run_dir / "iperf3_uplink_udp_5201.pcap"
            write_pcap(pcap)
            (run_dir / "tcpdump.stderr").write_text(
                "1 packet captured\n1 packet received by filter\n0 packets dropped by kernel\n",
                encoding="utf-8",
            )
            args = Namespace(
                run_dir=run_dir,
                pcap_processor=ROOT / "src/starlive_measure/trace/PcapBandwidthTrace.py",
                derived_dir=run_dir / "derived",
                port=5201,
                duration_ms=0,
                payload_bytes=100,
            )
            self.assertEqual(process_pcap_on_server(args, pcap), 0)
            metadata = json.loads((run_dir / "pcap_postprocess.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "complete")
            self.assertEqual(metadata["server_pcap"]["transfer"], "remote_only")
            self.assertGreater(metadata["server_pcap"]["size_bytes"], 0)
            self.assertTrue((run_dir / "derived" / "udp_arrival_time_ms.txt").is_file())
            self.assertTrue((run_dir / "derived" / "bandwidth_trace.tsv").is_file())

    def test_downlink_server_pcap_uses_source_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            pcap = run_dir / "iperf3_downlink_udp_5201.pcap"
            write_pcap(pcap, source_port=5201, destination_port=40000)
            (run_dir / "tcpdump.stderr").write_text(
                "1 packet captured\n1 packet received by filter\n0 packets dropped by kernel\n",
                encoding="utf-8",
            )
            args = Namespace(
                run_dir=run_dir,
                pcap_processor=ROOT / "src/starlive_measure/trace/PcapBandwidthTrace.py",
                derived_dir=run_dir / "derived",
                port=5201,
                duration_ms=0,
                payload_bytes=100,
                direction="downlink",
            )
            self.assertEqual(process_pcap_on_server(args, pcap), 0)
            metadata = json.loads((run_dir / "pcap_postprocess.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "complete")
            self.assertEqual(metadata["summary"]["packet_count"], 1)


if __name__ == "__main__":
    unittest.main()
