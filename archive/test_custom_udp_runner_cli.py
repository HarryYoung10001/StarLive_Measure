#!/usr/bin/env python3
"""CLI and dry-run regression tests for custom UDP runners."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_DIR / "traces" / "scripts" / "_archived"
TRACE_SCRIPT_DIR = REPO_DIR / "traces" / "scripts"


def run_script(script: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT_DIR / script), *args],
        cwd=REPO_DIR,
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


class CustomUdpRunnerCliTest(unittest.TestCase):
    def test_all_runners_document_no_icmp(self) -> None:
        scripts = (
            "run_custom_udp_latency_uplink.sh",
            "run_custom_udp_latency_uplink_sweep.sh",
            "run_iperf_uplink_rate_compare.sh",
            "run_custom_udp_latency_uplink_repeat.sh",
            "run_custom_udp_latency_downlink.sh",
            "run_custom_udp_latency_downlink_sweep.sh",
            "run_custom_udp_latency_downlink_repeat.sh",
        )
        for script in scripts:
            with self.subTest(script=script):
                result = run_script(script, "--help")
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn("--no-icmp", result.stdout)

    def test_unknown_option_is_rejected(self) -> None:
        result = run_script("run_custom_udp_latency_uplink_repeat.sh", "--not-an-option")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown option", result.stdout)

    def test_downlink_dry_run_omits_icmp_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_script(
                "run_custom_udp_latency_downlink.sh",
                "--no-icmp", "192.0.2.1", "1", "100M",
                env={"TRACE_LOG_ROOT": temp_dir, "DRY_RUN": "1"},
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            run_dir = next(Path(temp_dir).iterdir())
            self.assertFalse((run_dir / "icmp_phone").exists())
            metadata = (run_dir / "metadata.txt").read_text(encoding="utf-8")
            plan = (run_dir / "plan.txt").read_text(encoding="utf-8")
            self.assertIn("icmp_enabled=false", metadata)
            self.assertIn("ping_count=0", metadata)
            self.assertIn("receive_api=auto", metadata)
            self.assertIn("receive_api_requested=auto", metadata)
            self.assertIn("receive_capture_bytes=256", metadata)
            self.assertIn("icmp_status=skipped", plan)
            self.assertNotIn("icmp_command=", plan)

    def test_downlink_sweep_passes_no_icmp_and_rejects_resume_mode_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_script(
                "run_custom_udp_latency_downlink_sweep.sh",
                "--no-icmp", "192.0.2.1", "1",
                env={
                    "TRACE_LOG_ROOT": temp_dir,
                    "TRACE_SESSION_NAME": "single_udp",
                    "UDP_RATES": "100M",
                    "DRY_RUN": "1",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            session = Path(temp_dir)
            self.assertIn("icmp_enabled=false", (session / "sweep_metadata.txt").read_text(encoding="utf-8"))

            old_session = Path(temp_dir) / "old_session"
            old_session.mkdir()
            (old_session / "sweep_metadata.txt").write_text(
                "session_name=old\nicmp_enabled=true\n", encoding="utf-8"
            )
            (old_session / "sweep_status.tsv").write_text(
                "rate\tstart_utc\tend_utc\texit_code\n", encoding="utf-8"
            )
            mismatch = run_script(
                "run_custom_udp_latency_downlink_sweep.sh",
                "--no-icmp", "--resume", str(old_session), "192.0.2.1", "1",
                env={"DRY_RUN": "1", "UDP_RATES": "100M"},
            )
            self.assertEqual(mismatch.returncode, 2, mismatch.stdout)
            self.assertIn("ICMP mode does not match", mismatch.stdout)

    def test_downlink_repeat_dry_run_uses_compact_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_script(
                "run_custom_udp_latency_downlink_repeat.sh",
                "--no-icmp", "192.0.2.1", "1", "100pps", "1",
                env={
                    "TRACE_LOG_ROOT": temp_dir,
                    "DRY_RUN": "1",
                    "UDP_POSTPROCESS": "latency",
                    "EXPECTED_WIFI_SSID": "STARLINK",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            plans = list(Path(temp_dir).glob("dual_udp_latency_downlink_*/plan.txt"))
            self.assertEqual(len(plans), 1)
            self.assertIn("--output-mode latency", plans[0].read_text(encoding="utf-8"))
            metadata = (Path(temp_dir) / "repeat_metadata.txt").read_text(encoding="utf-8")
            self.assertIn("expected_wifi_ssid=STARLINK", metadata)
            header = (Path(temp_dir) / "repeat_status.tsv").read_text(encoding="utf-8").splitlines()[0]
            self.assertTrue(header.endswith("wifi_ssid\twifi_bssid\twifi_rssi\twifi_link_speed_mbps\tpreflight_file"))

    def test_downlink_repeat_ssid_guard_times_out_before_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "bin"
            fake_bin.mkdir()
            (fake_bin / "adb").write_text(
                """#!/usr/bin/env bash
if [[ "$*" == devices* ]]; then
  printf 'List of devices attached\n127.0.0.1:51468\tdevice\n'
elif [[ "$*" == *'getprop ro.product.cpu.abilist'* ]]; then
  printf 'arm64-v8a\n'
elif [[ "$*" == *'getprop sys.boot_completed'* ]]; then
  printf '1\n'
elif [[ "$*" == *'getprop ro.product.model'* ]]; then
  printf 'SM-S908U1\n'
elif [[ "$*" == *'dumpsys wifi'* ]]; then
  printf 'mWifiInfo SSID: "WRONG_NETWORK", BSSID: 00:11:22:33:44:55, RSSI: -42, Link speed: 1200Mbps\n'
fi
""",
                encoding="utf-8",
            )
            (fake_bin / "ssh").write_text(
                "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
            )
            for binary in (fake_bin / "adb", fake_bin / "ssh"):
                binary.chmod(0o755)

            result = run_script(
                "run_custom_udp_latency_downlink_repeat.sh",
                "--no-icmp", "192.0.2.1", "1", "100M", "1",
                env={
                    "TRACE_LOG_ROOT": temp_dir,
                    "ADB_UDP_DEVICE": "127.0.0.1:51468",
                    "SERVER_SSH": "sgp-server",
                    "EXPECTED_WIFI_SSID": "STARLINK",
                    "PREFLIGHT_WAIT_S": "1",
                    "PREFLIGHT_POLL_S": "1",
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                },
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            metadata = (Path(temp_dir) / "repeat_metadata.txt").read_text(encoding="utf-8")
            self.assertIn("expected_wifi_ssid=STARLINK", metadata)
            status_lines = (Path(temp_dir) / "repeat_status.tsv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(status_lines), 2)
            self.assertIn("preflight_failed", status_lines[1])
            self.assertIn("WRONG_NETWORK", status_lines[1])
            self.assertTrue(list(Path(temp_dir).glob("preflight_failed_*.txt")))
            self.assertFalse(list(Path(temp_dir).glob("dual_udp_latency_downlink_*")))

    def test_uplink_repeat_model_and_ssid_guards_stop_before_traffic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "bin"
            fake_bin.mkdir()
            (fake_bin / "adb").write_text(
                """#!/usr/bin/env bash
if [[ "$*" == devices* ]]; then
  printf 'List of devices attached\n127.0.0.1:53952\\tdevice\\n'
elif [[ "$*" == *'getprop ro.product.cpu.abilist'* ]]; then
  printf 'arm64-v8a\\n'
elif [[ "$*" == *'getprop sys.boot_completed'* ]]; then
  printf '1\\n'
elif [[ "$*" == *'getprop ro.product.model'* ]]; then
  printf 'WRONG_MODEL\\n'
elif [[ "$*" == *'dumpsys wifi'* ]]; then
  printf 'mWifiInfo SSID: "WRONG_NETWORK", BSSID: 00:11:22:33:44:55, RSSI: -42, Link speed: 1200Mbps\\n'
fi
""",
                encoding="utf-8",
            )
            (fake_bin / "ssh").write_text(
                """#!/usr/bin/env bash
case "$*" in
  *'uname -s -m'*) printf 'Linux x86_64\\n' ;;
  *'ss -H -lun'*) ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            for binary in (fake_bin / "adb", fake_bin / "ssh"):
                binary.chmod(0o755)

            result = run_script(
                "run_custom_udp_latency_uplink_repeat.sh",
                "--no-icmp", "192.0.2.1", "1", "100M", "1",
                env={
                    "TRACE_LOG_ROOT": temp_dir,
                    "ADB_UDP_DEVICE": "127.0.0.1:53952",
                    "SERVER_SSH": "sgp-server",
                    "EXPECTED_WIFI_SSID": "STARLINK",
                    "EXPECTED_DEVICE_MODEL": "RMX1941",
                    "PREFLIGHT_WAIT_S": "1",
                    "PREFLIGHT_POLL_S": "1",
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                },
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            metadata = (Path(temp_dir) / "repeat_metadata.txt").read_text(encoding="utf-8")
            self.assertIn("expected_wifi_ssid=STARLINK", metadata)
            self.assertIn("expected_device_model=RMX1941", metadata)
            status_lines = (Path(temp_dir) / "repeat_status.tsv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(status_lines), 2)
            self.assertIn("preflight_failed", status_lines[1])
            self.assertIn("WRONG_NETWORK", status_lines[1])
            self.assertFalse(list(Path(temp_dir).glob("dual_udp_latency_uplink_*")))

    def test_uplink_runner_documents_compact_receive_configuration(self) -> None:
        result = run_script("run_custom_udp_latency_uplink.sh", "--help")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("payload_bytes", result.stdout)

    def test_uplink_rate_sweep_dry_run_records_five_rates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_script(
                "run_custom_udp_latency_uplink_sweep.sh",
                "--no-icmp", "192.0.2.1", "30",
                env={
                    "TRACE_LOG_ROOT": temp_dir,
                    "ADB_UDP_DEVICE": "dry-run-device",
                    "UDP_RATES": "10M 20M 30M 40M 50M",
                    "UDP_POSTPROCESS": "arrival",
                    "UDP_DURATION_MODE": "wall-clock",
                    "UDP_RECEIVE_API": "auto",
                    "UDP_REQUIRE_RECEIVE_API": "recvmmsg",
                    "DRY_RUN": "1",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            session = Path(temp_dir)
            metadata = (session / "sweep_metadata.txt").read_text(encoding="utf-8")
            self.assertIn("rates=10M 20M 30M 40M 50M", metadata)
            self.assertIn("duration_mode=wall-clock", metadata)
            self.assertIn("icmp_enabled=false", metadata)
            summary_lines = (session / "sweep_summary.tsv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(summary_lines), 6)
            self.assertEqual([line.split("\t", 1)[0] for line in summary_lines[1:]],
                             ["10M", "20M", "30M", "40M", "50M"])
            self.assertIn("status=dry_run", (session / "session_summary.txt").read_text(encoding="utf-8"))

    def test_iperf_uplink_compare_dry_run_records_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_script(
                "run_iperf_uplink_rate_compare.sh",
                "--no-icmp", "192.0.2.1", "30",
                env={
                    "TRACE_LOG_ROOT": temp_dir,
                    "ADB_UDP_DEVICE": "dry-run-device",
                    "DRY_RUN": "1",
                    "COOLDOWN_S": "0",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            session = Path(temp_dir)
            metadata = (session / "compare_metadata.txt").read_text(encoding="utf-8")
            self.assertIn("udp_payload_len=1472", metadata)
            self.assertIn("duration_s=30", metadata)
            self.assertIn("icmp_enabled=false", metadata)
            summary = (session / "compare_summary.tsv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(summary), 6)
            self.assertEqual([line.split("\t", 1)[0] for line in summary[1:]],
                             ["10M", "20M", "30M", "40M", "50M"])
            self.assertIn("status=dry_run", (session / "session_summary.txt").read_text(encoding="utf-8"))

    def test_iperf_compare_parser_uses_receiver_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = root / "client.json"
            server = root / "server.json"
            intervals = root / "intervals.tsv"
            metrics = root / "metrics.txt"
            client.write_text(
                '{"end":{"sum_sent":{"seconds":30.0,"bits_per_second":10000000,"packets":100}}}',
                encoding="utf-8",
            )
            server.write_text(
                '{"intervals":['
                '{"sum":{"sender":false,"start":0,"end":1,"seconds":1,"bits_per_second":8000000,"packets":10,"lost_packets":10,"lost_percent":50}},'
                '{"sum":{"sender":true,"start":0,"end":1,"seconds":1,"bits_per_second":9000000,"packets":100,"lost_packets":0,"lost_percent":0}}'
                ']}',
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "python3", str(TRACE_SCRIPT_DIR / "IperfUplinkCompare.py"),
                    "--client-json", str(client), "--server-json", str(server),
                    "--intervals-output", str(intervals), "--metrics-output", str(metrics),
                ],
                cwd=REPO_DIR,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            values = metrics.read_text(encoding="utf-8")
            self.assertIn("server_interval_rows=1", values)
            self.assertIn("server_total_packets=10", values)
            self.assertIn("server_reported_lost_packets=10", values)
            self.assertIn("server_lost_packets=90", values)
            self.assertIn("server_loss_percent=90.000000000", values)
            self.assertIn("server_received_mbps_mean=8.000000000", values)
            self.assertEqual(len(intervals.read_text(encoding="utf-8").splitlines()), 2)

    def test_downlink_repeat_resumes_existing_status_without_resetting_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = Path(temp_dir)
            header = (
                "attempt\tsuccess_index\trun_id\tstart_utc\tend_utc\texit_code\tduration_mode"
                "\tscheduled_packets\tattempted_packets\tsent_packets\tsend_failed_packets"
                "\tsender_elapsed_s\tactual_payload_mbps\treceived_packets\tarrival_rows\tdelay_rows"
                "\ttime_rows\tempty_windows\trecv_span_ms\tclock_sync_samples\tstatus\treason"
                "\ticmp_enabled\ticmp_status\twifi_ssid\twifi_bssid\twifi_rssi"
                "\twifi_link_speed_mbps\tpreflight_file\n"
            )
            row = (
                "1\t1\tdual_udp_latency_downlink_existing\tstart\tend\t0\twall-clock"
                "\t1\t1\t1\t0\t1\t1\t1\t1\t0\t0\t\t1\t5\tcomplete\told"
                "\tfalse\tskipped\tSTARLINK\tbssid\t-30\t1200\tpreflight_attempt_01.txt\n"
            )
            (session / "repeat_status.tsv").write_text(header + row, encoding="utf-8")
            (session / "repeat_metadata.txt").write_text(
                "type=custom_udp_latency_downlink_repeat\n", encoding="utf-8"
            )
            result = run_script(
                "run_custom_udp_latency_downlink_repeat.sh",
                "--no-icmp", "192.0.2.1", "1", "100pps", "2",
                env={
                    "TRACE_LOG_ROOT": temp_dir,
                    "DRY_RUN": "1",
                    "EXPECTED_WIFI_SSID": "STARLINK",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(len((session / "repeat_status.tsv").read_text(encoding="utf-8").splitlines()), 2)
            metadata = (session / "repeat_metadata.txt").read_text(encoding="utf-8")
            self.assertIn("resumed_utc=", metadata)
            self.assertIn("resume_expected_wifi_ssid=STARLINK", metadata)


if __name__ == "__main__":
    unittest.main()
