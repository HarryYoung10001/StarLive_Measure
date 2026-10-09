#!/usr/bin/env python3
"""Post-process runs created by ``trace_runner.py``.

The runner performs best-effort post-processing after a live run.  This command
is safe to rerun later against a run or an entire session and never starts
ADB, SSH, iperf, or UDP services.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import shutil
import subprocess
import sys

from .PcapBandwidthTrace import process_run_pcap
from .Bandwidth100ms import process_run as process_bandwidth_100ms
from starlive_measure.paths import REPO_ROOT


ROOT = REPO_ROOT


def load(path: Path) -> tuple[dict, dict]:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    profile = json.loads((path / "profile.json").read_text(encoding="utf-8"))
    return manifest, profile


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def copy_first(candidates: list[Path], output: Path) -> bool:
    for candidate in candidates:
        if candidate.exists():
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, output)
            return True
    return False


def write_iperf_intervals(source: Path, output: Path) -> bool:
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    rows: list[dict[str, object]] = []
    for index, interval in enumerate(data.get("intervals", [])):
        if not isinstance(interval, dict) or not isinstance(interval.get("sum"), dict):
            continue
        summary = interval["sum"]
        if summary.get("sender") is not False:
            continue
        rows.append({
            "interval_index": index,
            "start_s": summary.get("start", ""),
            "end_s": summary.get("end", ""),
            "seconds": summary.get("seconds", ""),
            "bits_per_second": summary.get("bits_per_second", ""),
            "megabits_per_second": (float(summary.get("bits_per_second", 0)) / 1_000_000),
            "jitter_ms": summary.get("jitter_ms", ""),
            "lost_packets": summary.get("lost_packets", ""),
            "packets": summary.get("packets", ""),
            "lost_percent": summary.get("lost_percent", ""),
        })
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        fields = list(rows[0]) if rows else ["interval_index", "start_s", "end_s", "seconds", "bits_per_second", "megabits_per_second", "jitter_ms", "lost_packets", "packets", "lost_percent"]
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    return bool(rows)


def process(run_dir: Path) -> int:
    try:
        manifest, profile = load(run_dir)
    except (OSError, json.JSONDecodeError, KeyError) as error:
        print(f"ERROR: invalid trace run {run_dir}: {error}", file=sys.stderr)
        return 2
    workload = profile["workload"]
    derived = run_dir / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    failures = 0
    if workload["kind"] == "iperf3" and workload["direction"] == "uplink":
        client = run_dir / "roles" / "udp_sender" / "iperf3_client.json"
        server = run_dir / "roles" / "server" / "iperf3_server.json"
        if client.exists() and server.exists():
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "traces" / "scripts" / "IperfUplinkCompare.py"),
                    "--client-json", str(client), "--server-json", str(server),
                    "--intervals-output", str(derived / "iperf_intervals.tsv"),
                    "--metrics-output", str(derived / "iperf_metrics.txt"),
                ],
                check=False,
            )
            failures += int(result.returncode not in (0, 3))
        if profile.get("server", {}).get("tcpdump", {}).get("enabled", False):
            server_pull_rc = manifest.get("artifacts", {}).get("server_pull_rc")
            server_side = manifest.get("pcap_postprocess", {}).get("server_side", False)
            required = (
                run_dir / "derived" / "udp_arrival_time_ms.txt",
                run_dir / "derived" / "bandwidth_trace.tsv",
                run_dir / "derived" / "pcap_bandwidth_summary.json",
            )
            if server_side:
                pcap_result = {
                    "status": "complete"
                    if server_pull_rc == 0
                    and manifest.get("pcap_postprocess", {}).get("status") == "complete"
                    and all(path.is_file() and path.stat().st_size > 0 for path in required)
                    else "failed",
                    "error": "server-side PCAP artifacts are incomplete",
                }
            elif server_pull_rc not in (None, 0):
                pcap_result = {
                    "status": "failed",
                    "error": f"server artifact pull failed with exit code {server_pull_rc}",
                }
            else:
                pcap_result = process_run_pcap(run_dir, profile)
            manifest.setdefault("artifacts", {}).update(pcap_result.get("artifacts", {}))
            manifest["pcap_postprocess"] = {
                **manifest.get("pcap_postprocess", {}),
                "status": pcap_result.get("status"),
                "error": pcap_result.get("error", ""),
            }
            write_json(run_dir / "manifest.json", manifest)
            failures += int(pcap_result.get("status") != "complete")
    if workload["kind"] == "iperf3" and workload["direction"] == "downlink":
        source = run_dir / "roles" / "udp_receiver" / "iperf3_client.json"
        if not source.exists():
            source = run_dir / "roles" / "udp_receiver" / "remote" / "iperf3_client.json"
        if source.exists():
            shutil.copy2(source, derived / "iperf3_client.json")
            write_iperf_intervals(source, derived / "iperf_intervals.tsv")
    if workload["kind"] == "udp_flow":
        role = "udp_sender" if workload["direction"] == "uplink" else "udp_receiver"
        source_dir = run_dir / "roles" / role
        server_dir = run_dir / "roles" / "server"
        measurement_mode = manifest.get("collection_mode", "trace") == "measurement"
        if workload.get("postprocess") == "both":
            required = (
                derived / "udp_arrival_time_ms.txt",
                derived / "delay.txt",
                derived / "delay_window_time_ms.txt",
                derived / "delay_summary.txt",
                derived / "bandwidth_trace.tsv",
                derived / "custom_udp_trace_summary.json",
            )
            custom = manifest.get("custom_udp", {})
            try:
                summary_data = json.loads(
                    (derived / "custom_udp_trace_summary.json").read_text(encoding="utf-8")
                )
                capture_span_ms = float(summary_data.get("capture_duration_ms", 0.0))
                requested_ms = float(workload.get("duration_s", 0)) * 1000
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                summary_data = {}
                capture_span_ms = 0.0
                requested_ms = 0.0
            complete = (
                manifest.get("status") == "complete"
                and manifest.get("artifacts", {}).get("server_pull_rc") == 0
                and custom.get("status") == "complete"
                and requested_ms > 0
                and requested_ms * 0.833 <= capture_span_ms <= requested_ms * 1.167
                and all(path.is_file() and path.stat().st_size > 0 for path in required)
            )
            if not complete:
                failures += 1
            else:
                if not measurement_mode:
                    plot_python = ROOT / ".venv" / "bin" / "python"
                    if not plot_python.is_file():
                        plot_python = Path(sys.executable)
                    plot = derived / "bandwidth_time_scatter.png"
                    if not plot.is_file():
                        result = subprocess.run(
                            [
                                str(plot_python), str(Path(__file__).with_name("BandwidthTimeScatter.py")),
                                str(derived / "udp_arrival_time_ms.txt"), "--payload-bytes",
                                str(workload.get("payload_bytes", 1472)), "--window-ms",
                                str(max(1, round(float(workload.get("window_ms", 10))))), "--duration-ms",
                                str(round(float(workload.get("duration_s", 0)) * 1000)), "--output", str(plot),
                            ],
                            check=False, capture_output=True, text=True,
                        )
                        if result.returncode != 0:
                            manifest.setdefault("custom_udp", {})["plot_error"] = result.stderr.strip()
                    if plot.is_file():
                        manifest.setdefault("artifacts", {})["bandwidth_time_scatter"] = {
                            "path": str(plot.relative_to(run_dir)), "exists": True,
                            "size_bytes": plot.stat().st_size,
                        }
                        summary_data = {**summary_data, "plot_file": str(plot.relative_to(run_dir)), "plot_error": ""}
                        write_json(derived / "custom_udp_trace_summary.json", summary_data)
                    delay_plot = derived / "time_delay_scatter.png"
                    if not delay_plot.is_file():
                        delay_plot_result = subprocess.run(
                            [str(plot_python), str(ROOT / "traces" / "scripts" / "DelayTimeScatter.py"), str(derived), "--output", str(delay_plot)],
                            check=False, capture_output=True, text=True,
                        )
                        if delay_plot_result.returncode != 0:
                            manifest.setdefault("custom_udp", {})["delay_plot_error"] = delay_plot_result.stderr.strip()
                    if delay_plot.is_file():
                        manifest.setdefault("artifacts", {})["time_delay_scatter"] = {
                            "path": str(delay_plot.relative_to(run_dir)), "exists": True,
                            "size_bytes": delay_plot.stat().st_size,
                        }
                        write_json(derived / "custom_udp_trace_summary.json", {
                            **summary_data, "delay_plot_file": str(delay_plot.relative_to(run_dir)), "delay_plot_error": "",
                        })
        names = (
            "udp_arrival_time_ms.txt", "delay.txt", "delay_window_time_ms.txt",
            "delay_summary.txt", "udp_receive_gap_report.tsv",
            "uplink_udp_receiver_summary.txt", "downlink_udp_receiver_summary.txt",
        )
        for name in names:
            copy_first([source_dir / name, source_dir / "remote" / name, server_dir / name], derived / name)
        if (derived / "bandwidth_trace.tsv").is_file() or (derived / "udp_arrival_time_ms.txt").is_file():
            try:
                process_bandwidth_100ms(
                    run_dir,
                    payload_bytes=int(workload.get("payload_bytes", 0) or 0) or None,
                    duration_ms=float(workload.get("duration_s", 0) or 0) * 1000 or None,
                )
            except (OSError, ValueError, ImportError) as error:
                manifest.setdefault("custom_udp", {})["bandwidth_100ms_error"] = str(error)
    icmp = manifest.get("icmp", {})
    ping = run_dir / "roles" / "icmp_phone" / "ping.log"
    if icmp.get("enabled") and ping.exists() and icmp.get("count"):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "traces" / "scripts" / "ICMPPingSummaryProcess.py"),
                str(ping), "--expected-transmitted", str(icmp["count"]),
                "--output", str(derived / "icmp_summary.txt"),
            ],
            check=False,
        )
        failures += int(result.returncode != 0)
    write_json(run_dir / "manifest.json", manifest)
    print(f"Processed {run_dir}")
    return 0 if failures == 0 else 1


def find_runs(path: Path) -> list[Path]:
    if (path / "manifest.json").is_file():
        return [path]
    return sorted(candidate.parent for candidate in path.rglob("manifest.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="trace run or session directory")
    args = parser.parse_args()
    runs = find_runs(args.input)
    if not runs:
        print(f"ERROR: no manifest.json found below {args.input}", file=sys.stderr)
        return 2
    statuses = [process(run) for run in runs]
    return 0 if all(status == 0 for status in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
