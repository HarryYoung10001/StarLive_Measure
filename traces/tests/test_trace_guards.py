from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from traces.scripts.trace_runtime import (
    latency_statistics,
    parse_rate_mbps,
    parse_rate_pps,
    parse_route_info,
    parse_wifi_info,
    validate_custom_run_success,
)


class TraceGuardTest(unittest.TestCase):
    def test_android_wifi_and_route_parsers(self) -> None:
        wifi = parse_wifi_info(
            "mWifiInfo SSID: STARLINK, BSSID: 66:A4:3F:27:D4:76, "
            "RSSI: -55, Link speed: 72Mbps"
        )
        self.assertEqual(wifi["ssid"], "STARLINK")
        self.assertEqual(wifi["bssid"], "66:a4:3f:27:d4:76")
        self.assertEqual(parse_route_info("178.128.213.115 via 192.168.1.1 dev wlan0 src 192.168.1.219"), {
            "gateway": "192.168.1.1", "interface": "wlan0", "source": "192.168.1.219",
        })

    def test_latency_statistics(self) -> None:
        stats = latency_statistics([1, 2, 3, 4, 5])
        self.assertEqual(stats["valid_rows"], 5)
        self.assertEqual(stats["mean_ms"], 3)
        self.assertEqual(stats["p95_ms"], 4.8)

    def test_pps_rate_is_converted_using_payload_size(self) -> None:
        self.assertEqual(parse_rate_pps("300pps", 1200), 300.0)
        self.assertAlmostEqual(parse_rate_mbps("300pps", 1200), 2.88)

    def test_custom_success_strict_span_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / "derived").mkdir()
            for name in ("udp_arrival_time_ms.txt", "delay.txt", "delay_window_time_ms.txt", "delay_summary.txt", "bandwidth_trace.tsv"):
                (run / "derived" / name).write_text("1\n", encoding="utf-8")
            (run / "derived" / "time_delay_scatter.png").write_bytes(b"png")
            (run / "derived" / "custom_udp_trace_summary.json").write_text(json.dumps({
                "capture_duration_ms": 145000,
                "latency": {"window_ms": 10},
            }), encoding="utf-8")
            (run / "manifest.json").write_text(json.dumps({
                "status": "complete",
                "custom_udp": {
                    "status": "complete",
                    "server_utrc": {"transfer": "remote_only", "exists": True, "size_bytes": 10},
                    "sender": {"done_ack": True, "clock_sync_samples": 1, "sent_packets": 1},
                },
                "artifacts": {"server_pull_rc": 0, "phone_pull_rc": 0},
            }), encoding="utf-8")
            self.assertTrue(validate_custom_run_success(run, min_span_s=145, max_span_s=160))
            (run / "derived" / "custom_udp_trace_summary.json").write_text(json.dumps({
                "capture_duration_ms": 144999,
                "latency": {"window_ms": 10},
            }), encoding="utf-8")
            self.assertFalse(validate_custom_run_success(run, min_span_s=145, max_span_s=160))


if __name__ == "__main__":
    unittest.main()
