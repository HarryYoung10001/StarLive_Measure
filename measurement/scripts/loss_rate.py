#!/usr/bin/env python3
"""Calculate UDP loss totals and gap-based 100 ms loss traces."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

try:
    from common import find_runs, load_manifest, load_profile, precise_rows, read_kv
except ImportError:  # package-style import
    from .common import find_runs, load_manifest, load_profile, precise_rows, read_kv


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _summary_files(run: Path) -> list[Path]:
    preferred = [
        run / "roles" / "server" / "downlink_udp_sender_summary.txt",
        run / "roles" / "server" / "uplink_udp_receiver_summary.txt",
        run / "roles" / "udp_sender" / "uplink_udp_sender_summary.txt",
        run / "roles" / "udp_sender" / "remote" / "uplink_udp_sender_summary.txt",
    ]
    return [path for path in preferred if path.is_file()] + [
        path for path in sorted(run.rglob("*summary.txt")) if path not in preferred
    ]


def _sender_values(run: Path) -> tuple[dict[str, str], str]:
    for path in _summary_files(run):
        values = read_kv(path)
        if any(key in values for key in ("attempted_packets", "sent_packets", "scheduled_packets")):
            return values, str(path)
    manifest = load_manifest(run)
    sender = manifest.get("custom_udp", {}).get("sender", {})
    return (sender if isinstance(sender, dict) else {}), "manifest.custom_udp.sender"


def _receiver_values(run: Path) -> tuple[dict[str, str], str]:
    preferred = [
        run / "roles" / "udp_receiver" / "downlink_udp_receiver_summary.txt",
        run / "roles" / "server" / "uplink_udp_receiver_summary.txt",
    ]
    for path in preferred + sorted(run.rglob("*receiver_summary.txt")):
        if not path.is_file():
            continue
        values = read_kv(path)
        if any(key in values for key in ("received_packets", "unique_received_packets", "capture_records")):
            return values, str(path)
    summary = load_manifest(run).get("custom_udp", {}).get("receiver_summary", {})
    return (summary if isinstance(summary, dict) else {}), "manifest.custom_udp.receiver_summary"


def summarize_run(run: Path) -> dict[str, Any]:
    sender, sender_source = _sender_values(run)
    receiver, receiver_source = _receiver_values(run)
    denominator_key = next((key for key in ("attempted_packets", "sent_packets", "scheduled_packets") if _int(sender.get(key)) > 0), "")
    sent = _int(sender.get(denominator_key)) if denominator_key else 0
    received = _int(receiver.get("unique_received_packets", receiver.get("received_packets", receiver.get("capture_records", 0))))
    if not received:
        received = len({row.get("seq") for row in precise_rows(run) if row.get("seq")})
    lost = max(0, sent - received) if sent else 0
    sequence_gap = _int(receiver.get("sequence_gap_packets", 0))
    custom_summary = load_manifest(run).get("custom_udp", {}).get("summary", {})
    if not sequence_gap and isinstance(custom_summary, dict):
        sequence_gap = _int(custom_summary.get("sequence_gap_packets", 0))
    return {
        "run_id": run.name,
        "direction": load_profile(run).get("workload", {}).get("direction", ""),
        "sent_packets": sent,
        "received_packets": received,
        "lost_packets": lost,
        "loss_rate": (lost / sent if sent else None),
        "denominator": denominator_key or "unavailable",
        "sender_source": sender_source,
        "receiver_source": receiver_source,
        "sequence_gap_packets": sequence_gap,
        "duplicate_packets": _int(receiver.get("duplicate_packets", 0)),
        "socket_rxq_overflow_packets": _int(receiver.get("socket_rxq_overflow_packets", receiver.get("socket_drops", 0))),
    }


def _gap_rows(run: Path) -> list[dict[str, str]]:
    path = run / "derived" / "udp_receive_gap_report.tsv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_loss_trace(run: Path, summary: dict[str, Any], bin_ms: float = 100.0) -> Path:
    if bin_ms <= 0:
        raise ValueError("bin_ms must be positive")
    precise = precise_rows(run)
    first_epoch = None
    if precise:
        try:
            first_epoch = int(precise[0]["receive_unix_ns"]) / 1e9
        except (KeyError, TypeError, ValueError):
            first_epoch = None
    gaps = _gap_rows(run)
    bins: dict[int, int] = {}
    attributed = 0
    total_lost = _int(summary.get("lost_packets"))
    basis = "exact_total_zero" if total_lost == 0 else ("inferred_seq_gap_partial" if gaps else "unattributed_loss")
    if gaps and first_epoch is None:
        try:
            first_epoch = float(gaps[0]["start_recv_epoch_s"]) - float(gaps[0].get("prev_send_rel_s", 0) or 0)
        except (KeyError, TypeError, ValueError):
            first_epoch = 0.0
    for row in gaps:
        try:
            missing = max(0, int(row["seq_jump"]) - 1)
            relative_ms = (float(row["end_recv_epoch_s"]) - float(first_epoch or 0.0)) * 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        index = max(0, int(relative_ms // bin_ms))
        bins[index] = bins.get(index, 0) + missing
        attributed += missing
    if gaps and total_lost > 0:
        basis = "inferred_seq_gap" if attributed >= total_lost else "inferred_seq_gap_partial"
    max_index = max(bins, default=-1)
    duration = load_profile(run).get("workload", {}).get("duration_s")
    try:
        max_index = max(max_index, int(float(duration) * 1000.0 // bin_ms))
    except (TypeError, ValueError):
        pass
    output = run / "derived" / "loss_100ms.tsv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["time_ms", "sent_packets", "received_packets", "lost_packets", "loss_rate", "source"])
        total_sent = _int(summary.get("sent_packets"))
        total_received = _int(summary.get("received_packets"))
        for index in range(max_index + 1):
            lost = bins.get(index, 0)
            expected = round(total_sent * bin_ms / 1000.0 / max(float(duration or 0), 0.001)) if duration else 0
            if basis == "unattributed_loss":
                received = ""; lost_value = ""; rate = ""
            else:
                received = max(0, expected - lost); lost_value = lost
                rate = f"{(lost / expected if expected else 0.0):.9f}"
            writer.writerow([f"{index * bin_ms:.6f}", expected, received, lost_value, rate, basis])
    metadata = {"source": basis, "bin_ms": bin_ms, "gap_rows": len(gaps),
                "total_lost_packets": total_lost, "attributed_gap_packets": attributed,
                "unattributed_lost_packets": max(0, total_lost - attributed), "total_summary": summary}
    (run / "derived" / "loss_100ms_metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def process_run(run: Path) -> dict[str, Any]:
    summary = summarize_run(run)
    write_loss_trace(run, summary)
    output = run / "derived" / "loss_summary.tsv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerow(summary)
    (run / "derived" / "loss_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="run or session directory")
    args = parser.parse_args(argv)
    runs = find_runs(args.input)
    if not runs:
        parser.error(f"no run manifests found below {args.input}")
    for run in runs:
        print(json.dumps(process_run(run), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
