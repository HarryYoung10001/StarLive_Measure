from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from traces.scripts.trace_runtime import normalize_iperf_json


REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "traces" / "scripts" / "trace_runner.py"
PROFILES = REPO / "traces" / "profiles"


class TraceRunnerCliTest(unittest.TestCase):
    def run_cli(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        if env:
            command_env.update(env)
        return subprocess.run(
            [sys.executable, str(RUNNER), *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
            env=command_env,
        )

    def test_all_profiles_load_and_dry_run(self) -> None:
        for profile in sorted(PROFILES.glob("*.json")):
            with self.subTest(profile=profile.name), tempfile.TemporaryDirectory() as temp:
                if profile.name == "icmp_only.json":
                    roles = ["--role", "icmp_phone=icmp"]
                elif profile.name == "udp_echo_rtt.json":
                    roles = ["--role", "udp_echo_phone=echo"]
                elif "downlink" in profile.name:
                    roles = ["--role", "udp_receiver=udp", "--role", "icmp_phone=icmp", "--no-icmp"]
                else:
                    roles = ["--role", "udp_sender=udp", "--role", "icmp_phone=icmp", "--no-icmp"]
                result = self.run_cli(
                    "run", "--profile", str(profile), "--server-ip", "192.0.2.1",
                    "--server-ssh", "example", "--session-dir", temp, "--dry-run", *roles,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                runs = list(Path(temp).glob("*/manifest.json"))
                self.assertEqual(len(runs), 1)
                manifest = json.loads(runs[0].read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "planned")
                self.assertEqual(manifest["preflight"]["mode"], "dry-run")
                self.assertFalse(list(runs[0].parent.rglob("*runner.sh")))

    def test_missing_required_role_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_cli(
                "run", "--profile", str(PROFILES / "iperf_uplink_tcpdump.json"),
                "--server-ip", "192.0.2.1", "--session-dir", temp, "--dry-run",
                "--role", "icmp_phone=icmp",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("required role", result.stderr)

    def test_duplicate_device_roles_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_cli(
                "run", "--profile", str(PROFILES / "iperf_uplink_tcpdump.json"),
                "--server-ip", "192.0.2.1", "--session-dir", temp, "--dry-run",
                "--role", "udp_sender=same", "--role", "icmp_phone=same",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("distinct", result.stderr)

    def test_sweep_reuses_run_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_cli(
                "sweep", "--profile", str(PROFILES / "iperf_downlink_bandwidth.json"),
                "--server-ip", "192.0.2.1", "--session-dir", temp, "--dry-run",
                "--role", "udp_receiver=udp", "--no-icmp", "--rates", "10M,20M",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            session = Path(temp)
            runs = sorted(session.glob("*/manifest.json"))
            self.assertEqual(len(runs), 2)
            self.assertEqual(len((session / "runs.jsonl").read_text(encoding="utf-8").splitlines()), 2)
            rates = [json.loads(path.read_text(encoding="utf-8"))["workload"]["rate"] for path in runs]
            self.assertEqual(rates, ["10M", "20M"])

    def test_downlink_sweep_dry_run_records_all_rates_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "udp_downlink_rate_sweep_150s"
            result = self.run_cli(
                "sweep", "--profile", str(PROFILES / "udp_downlink_latency_bandwidth.json"),
                "--server-ip", "192.0.2.1", "--server-ssh", "example",
                "--server-repo", "/root/StarLive_Measure", "--role",
                "udp_receiver=127.0.0.1:64009", "--rates", "300pps,30M,40M,50M,60M,80M",
                "--duration", "150", "--interval", "0.2", "--payload", "1200",
                "--port", "5003", "--window-ms", "10", "--no-icmp",
                "--expected-device-model", "CPH1909", "--expected-wifi-ssid", "STARLINK",
                "--expected-wifi-bssid", "66:a4:3f:27:d4:76", "--expected-route-interface", "wlan0",
                "--expected-route-gateway", "192.168.1.1", "--min-span-s", "145",
                "--max-span-s", "160", "--max-attempts-per-rate", "3", "--cooldown-s", "30",
                "--session-dir", str(session), "--dry-run",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifests = sorted(session.glob("*/manifest.json"))
            self.assertEqual(len(manifests), 6)
            self.assertEqual(
                [json.loads(path.read_text(encoding="utf-8"))["workload"]["rate"] for path in manifests],
                ["300pps", "30M", "40M", "50M", "60M", "80M"],
            )
            plan = (session / "rate_sweep_plan.tsv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(plan), 7)
            summary = json.loads((session / "sweep_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["completed_rates"], 6)
            self.assertEqual(summary["attempts"], 6)
            self.assertAlmostEqual(float(plan[1].split("\t")[2]), 2.88)

    def test_uplink_repeat_applies_smoke_overrides_and_stays_in_one_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "iperf_uplink_tcpdump_30M_30s"
            result = self.run_cli(
                "repeat", "--profile", str(PROFILES / "iperf_uplink_tcpdump.json"),
                "--server-ip", "192.0.2.1", "--server-ssh", "sgp-server",
                "--server-repo", "/root/StarLive_Measure", "--role",
                "udp_sender=127.0.0.1:59653", "--rate", "30M", "--duration", "30",
                "--interval", "0.2", "--payload", "1200", "--port", "5201",
                "--no-icmp", "--successes", "1", "--max-attempts", "3",
                "--session-dir", str(session), "--dry-run",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifests = list(session.glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "planned")
            self.assertEqual(manifest["server"], {
                "ip": "192.0.2.1", "ssh": "sgp-server",
                "repo": "/root/StarLive_Measure", "port": 5201,
            })
            self.assertEqual(manifest["workload"]["rate"], "30M")
            self.assertEqual(manifest["workload"]["duration_s"], 30.0)
            self.assertFalse(manifest["icmp"]["enabled"])
            summary = json.loads((session / "repeat_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["successes"], 1)
            self.assertEqual(summary["attempts"], 1)
            runs = (session / "runs.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(runs), 1)

    def test_custom_both_repeat_records_80m_smoke_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "udp_uplink_custom_80M_30s"
            result = self.run_cli(
                "repeat", "--profile", str(PROFILES / "udp_uplink_latency_bandwidth.json"),
                "--server-ip", "178.128.213.115", "--server-ssh", "sgp-server",
                "--server-repo", "/root/StarLive_Measure", "--role",
                "udp_sender=127.0.0.1:57915", "--rate", "80M", "--duration", "30",
                "--interval", "0.2", "--payload", "1472", "--port", "5201",
                "--window-ms", "10", "--no-icmp", "--successes", "1",
                "--max-attempts", "3", "--session-dir", str(session), "--dry-run",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest_path = next(session.glob("*/manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["roles"]["udp_sender"], "127.0.0.1:57915")
            self.assertEqual(manifest["server"]["ssh"], "sgp-server")
            self.assertEqual(manifest["server"]["ip"], "178.128.213.115")
            self.assertEqual(manifest["workload"]["postprocess"], "both")
            self.assertEqual(manifest["workload"]["rate"], "80M")
            self.assertEqual(manifest["workload"]["duration_s"], 30.0)
            self.assertEqual(manifest["workload"]["payload_bytes"], 1472)
            self.assertFalse(manifest["icmp"]["enabled"])
            summary = json.loads((session / "repeat_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["successes"], 1)
            self.assertEqual(summary["attempts"], 1)

    def test_custom_both_repeat_records_300pps_long_latency_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "udp_uplink_custom_300pps_1200B_150s"
            result = self.run_cli(
                "repeat", "--profile", str(PROFILES / "udp_uplink_latency_bandwidth.json"),
                "--server-ip", "178.128.213.115", "--server-ssh", "sgp-server",
                "--server-repo", "/root/StarLive_Measure", "--role",
                "udp_sender=127.0.0.1:59199", "--rate", "300pps", "--duration", "150",
                "--interval", "0.2", "--payload", "1200", "--port", "5201",
                "--window-ms", "10", "--no-icmp", "--successes", "15",
                "--max-attempts", "20", "--cooldown-s", "30", "--min-span-s", "145",
                "--max-span-s", "160", "--session-dir", str(session), "--dry-run",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(next(session.glob("*/manifest.json")).read_text(encoding="utf-8"))
            self.assertEqual(manifest["roles"]["udp_sender"], "127.0.0.1:59199")
            self.assertEqual(manifest["workload"]["rate"], "300pps")
            self.assertEqual(manifest["workload"]["duration_s"], 150.0)
            self.assertEqual(manifest["workload"]["payload_bytes"], 1200)

    def test_repeat_resume_requires_explicit_device_environment_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "downlink_session"
            common = (
                "repeat", "--profile", str(PROFILES / "udp_downlink_latency_bandwidth.json"),
                "--server-ip", "192.0.2.1", "--server-ssh", "sgp-server",
                "--server-repo", "/root/StarLive_Measure", "--rate", "500M",
                "--duration", "150", "--payload", "1200", "--port", "5003",
                "--window-ms", "10", "--no-icmp", "--min-span-s", "145",
                "--max-span-s", "160", "--expected-wifi-ssid", "STARLINK",
                "--expected-wifi-bssid", "66:a4:3f:27:d4:76",
                "--expected-route-interface", "wlan0", "--expected-route-gateway", "192.168.1.1",
                "--session-dir", str(session), "--dry-run",
            )
            initial = self.run_cli(
                *common, "--role", "udp_receiver=old-phone", "--expected-device-model", "RMX1941",
                "--successes", "1", "--max-attempts", "1",
            )
            self.assertEqual(initial.returncode, 0, initial.stderr)
            denied = self.run_cli(
                *common, "--resume", "--role", "udp_receiver=new-phone", "--expected-device-model", "CPH1909",
                "--segment-successes", "1", "--segment-max-attempts", "1",
            )
            self.assertEqual(denied.returncode, 2)
            self.assertIn("environment mismatch", denied.stderr)
            migrated = self.run_cli(
                *common, "--resume", "--resume-environment-change",
                "--environment-change-reason", "replace phone", "--role", "udp_receiver=new-phone",
                "--expected-device-model", "CPH1909", "--segment-successes", "1",
                "--segment-max-attempts", "1",
            )
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            metadata = json.loads((session / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["environment_generation"], 2)
            self.assertEqual(metadata["active_environment"]["roles"]["udp_receiver"], "new-phone")
            changes = (session / "environment_changes.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(json.loads(row)["event"] == "environment_change" for row in changes))
            new_run = sorted(session.glob("*/manifest.json"))[-1]
            manifest = json.loads(new_run.read_text(encoding="utf-8"))
            self.assertEqual(manifest["environment"]["generation"], 2)
            self.assertEqual(manifest["environment"]["roles"]["udp_receiver"], "new-phone")
            summary = json.loads((session / "repeat_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["new_segment_successes"], 1)
            self.assertEqual(summary["new_segment_attempts"], 1)
            self.assertEqual(summary["new_segment_max_attempts"], 1)

    def test_repository_has_no_legacy_server_alias_outside_runtime_logs(self) -> None:
        for path in REPO.rglob("*"):
            relative_parts = path.relative_to(REPO).parts
            if (
                not path.is_file()
                or ".git" in relative_parts
                or "__pycache__" in relative_parts
                or "logs" in relative_parts
                or "data" in relative_parts
                or "archive" in relative_parts
            ):
                continue
            try:
                content = path.read_bytes()
            except OSError:
                continue
            self.assertNotIn(b"starlink" + b"-sg", content, str(path))

    def test_fake_adb_executes_icmp_profile_without_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            adb = fake_bin / "adb"
            adb.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == devices ]]; then\n"
                "  printf 'List of devices attached\\nphone-a\\tdevice\\n'\n"
                "else\n"
                "  printf '1 packets transmitted, 1 packets received, 0.0%% packet loss\\n'\n"
                "fi\n",
                encoding="utf-8",
            )
            adb.chmod(0o755)
            result = self.run_cli(
                "run", "--profile", str(PROFILES / "icmp_only.json"),
                "--server-ip", "192.0.2.1", "--server-ssh", "unused",
                "--session-dir", temp, "--role", "icmp_phone=phone-a",
                "--duration", "0.1", "--interval", "0.1",
                env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifests = list(temp_path.glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["icmp"]["status"], "complete")

    def test_normalize_iperf_json_discards_adb_transport_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "iperf3_client.json"
            path.write_text(
                "warning: JSON size of data read does not correspond to offered length\n"
                '{"end":{"sum":{"bytes":123}}}\n',
                encoding="utf-8",
            )
            data = normalize_iperf_json(path)
            self.assertEqual(data["end"]["sum"]["bytes"], 123)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), data)


if __name__ == "__main__":
    unittest.main()
