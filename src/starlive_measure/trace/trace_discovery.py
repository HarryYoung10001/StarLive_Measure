"""Find measurement runs in both legacy and trace-session log layouts."""

from __future__ import annotations

from pathlib import Path


DUAL_TRACE_PREFIXES = (
    "dual_icmp_heavy_udp_",
    "dual_udp_latency_downlink_",
    "dual_udp_latency_uplink_",
)


def dual_trace_run_dirs(logs_dir: Path) -> list[Path]:
    """Return physical dual-phone trace directories without symlink duplicates.

    The trace runner creates one directory per experiment and a ``latest_*``
    symlink.  Discovery is intentionally based on the directory name and
    metadata/files rather than every nested JSON file, so server-side copies do
    not become independent runs.
    """

    if not logs_dir.exists() or not logs_dir.is_dir():
        return []

    runs: dict[Path, Path] = {}
    for candidate in logs_dir.iterdir():
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        if not candidate.name.startswith(DUAL_TRACE_PREFIXES):
            continue

        metadata = candidate / "metadata.txt"
        ping_log = candidate / "icmp_phone" / "remote" / "ping.log"
        client_json = candidate / "heavy_udp_phone" / "remote" / "iperf3_client.json"
        server_json = candidate / "server" / "iperf3_server.json"
        udp_sender = candidate / "server" / "downlink_udp_sender.csv.gz"
        udp_receiver = candidate / "udp_phone" / "downlink_udp_receiver.csv"
        uplink_sender = candidate / "udp_phone" / "uplink_udp_sender.csv.gz"
        uplink_receiver = candidate / "server" / "uplink_udp_receiver.csv.gz"
        if not metadata.exists() and not (
            ping_log.exists()
            or client_json.exists()
            or server_json.exists()
            or udp_sender.exists()
            or udp_receiver.exists()
            or uplink_sender.exists()
            or uplink_receiver.exists()
        ):
            continue

        resolved = candidate.resolve()
        runs[resolved] = candidate

    return sorted(runs.values(), key=lambda path: path.name)


def capacity_run_dirs(logs_dir: Path) -> list[Path]:
    """Return run directories containing recognized iperf3 JSON inputs.

    Legacy capacity runs keep JSON directly in ``logs/<run>``.  The trace
    layout keeps downlink stream JSON below ``streams/stream_N`` and places
    uplink server JSON below ``capacity/server``; both are normalized to the
    directory that owns the run metadata.
    """

    run_dirs: set[Path] = set()
    for filename in (
        "iperf3_downlink_client.json",
        "iperf3_uplink_client.json",
        "iperf3_server.json",
        "iperf3_client.json",
    ):
        for json_path in logs_dir.rglob(filename):
            parent = json_path.parent
            if filename == "iperf3_client.json" and parent.parent.name == "streams":
                candidate = parent.parent.parent
                # adb pull keeps a second copy below <run>/remote/<run>/;
                # normalize that copy back to the owning Mac-side run.
                if candidate.parent.name == "remote":
                    candidate = candidate.parent.parent
                run_dirs.add(candidate)
            else:
                run_dirs.add(parent)
    return sorted(path for path in run_dirs if path.is_dir())


def fine_rtt_run_dirs(logs_dir: Path) -> list[Path]:
    """Return the owning directories for fine UDP RTT sample CSVs."""

    run_dirs: set[Path] = set()
    for sample_path in logs_dir.rglob("fine_rtt_samples.csv"):
        parent = sample_path.parent
        # Older pull-based runs store the remote probe directory below
        # <run>/remote/fine_rtt_udp_*/; trace runs keep it directly in rtt/.
        if parent.parent.name == "remote":
            run_dirs.add(parent.parent.parent)
        else:
            run_dirs.add(parent)
    return sorted(path for path in run_dirs if path.is_dir())


def ping_run_dirs(logs_dir: Path) -> list[Path]:
    """Return run directories containing Android ping logs."""

    run_dirs: set[Path] = set()
    for ping_path in logs_dir.rglob("ping_*.log"):
        parent = ping_path.parent
        if parent.parent.name == "remote":
            run_dirs.add(parent.parent.parent)
        else:
            run_dirs.add(parent)
    return sorted(path for path in run_dirs if path.is_dir())
