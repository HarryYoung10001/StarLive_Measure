"""Cross-rate analysis for dual-ended root UDP packet ledgers."""

from __future__ import annotations

import csv
import gzip
import json
import math
import statistics
from pathlib import Path
from typing import Any

from starlive_measure.root.packet_ledger import decode_failure_summary, iter_decoded_packets, read_manifest
from starlive_measure.trace.TraceDataProcess import SUMMARY_FIELDS, parse_ping_log, write_csv


def _percentile(values: list[float], percentage: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(percentage * len(ordered) / 100) - 1))]


def derive_reference_run(run_dir: Path, *, offered_rate: str, duration_s: float, server_ip: str,
                         uncertainty_limit_ms: float = 5.0) -> dict[str, Any]:
    """Verify both ledgers, pair iperf sequences, and write per-run metrics."""
    phone_dir = run_dir / "roles" / "udp_receiver" / "packet_ledger"
    server_dir = run_dir / "roles" / "server" / "packet_ledger"
    phone_manifest = read_manifest(phone_dir / "ledger_manifest.json")
    server_manifest = read_manifest(server_dir / "ledger_manifest.json")
    for label, item in (("phone", phone_manifest), ("server", server_manifest)):
        if item["direction"] != "source" or item["port"] != 5201:
            raise ValueError(f"{label} packet ledger has unexpected direction or port")
        if item["target_packets"] == 0 or item["iperf_decoded_records"] == 0:
            raise ValueError(f"{label} ledger has no decodable iperf sequence")

    sent: dict[int, dict[str, int]] = {}
    received: dict[int, dict[str, int]] = {}
    sent_duplicates = received_duplicates = 0
    for packet in iter_decoded_packets(server_dir, server_manifest):
        if packet["sequence"] in sent:
            sent_duplicates += 1
        else:
            sent[packet["sequence"]] = packet
    for packet in iter_decoded_packets(phone_dir, phone_manifest):
        if packet["sequence"] in received:
            received_duplicates += 1
        else:
            received[packet["sequence"]] = packet
    if not sent:
        raise ValueError("server ledger contains no unique iperf sequence")

    matched = sorted(sent.keys() & received.keys())
    delays_raw_ms = [(received[seq]["timestamp_ns"] - sent[seq]["timestamp_ns"]) / 1_000_000 for seq in matched]
    if not matched:
        raise ValueError("phone/server ledgers have no matching iperf sequences")
    run_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    clock = run_manifest.get("clock_sync", {})
    uncertainty_ms = clock.get("uncertainty_ms")
    absolute = isinstance(uncertainty_ms, (int, float)) and uncertainty_ms <= uncertainty_limit_ms
    if absolute:
        offset_ms = float(clock.get("offset_ms", 0.0))
        delays = [value - offset_ms for value in delays_raw_ms]
        latency_mode = "absolute_clock_corrected"
        baseline_ms = 0.0
    else:
        baseline_ms = float(_percentile(delays_raw_ms, 1) or 0.0)
        delays = [value - baseline_ms for value in delays_raw_ms]
        latency_mode = "relative_per_rate_p1_subtracted"
        offset_ms = None

    latency_path = run_dir / "derived" / "iperf_flow_latency_packets.tsv.gz"
    latency_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(latency_path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["sequence", "server_timestamp_ns", "phone_timestamp_ns", "server_payload_bytes",
                         "phone_payload_bytes", "raw_clock_delta_ms", "flow_latency_ms", "latency_mode"])
        for index, (sequence, delay) in enumerate(zip(matched, delays)):
            writer.writerow([sequence, sent[sequence]["timestamp_ns"], received[sequence]["timestamp_ns"],
                             sent[sequence]["payload_bytes"], received[sequence]["payload_bytes"],
                             f"{delays_raw_ms[index]:.9f}", f"{delay:.9f}", latency_mode])

    transmitted = len(sent)
    delivered = len(matched)
    lost = max(0, transmitted - delivered)
    receiver_bytes = sum(received[sequence]["payload_bytes"] for sequence in matched)
    server_unique_payload_bytes = sum(packet["payload_bytes"] for packet in sent.values())
    receive_origin_ns = min(packet["timestamp_ns"] for packet in received.values())
    active_span_s = max(0.0, (max(packet["timestamp_ns"] for packet in received.values()) - receive_origin_ns) / 1e9)
    matched_bytes = sum(received[sequence]["payload_bytes"] for sequence in matched)
    active_bins = {max(0, (received[sequence]["timestamp_ns"] - receive_origin_ns) // 10_000_000) for sequence in matched}
    active_window_duration_s = len(active_bins) * 0.010
    ping_path = run_dir / "roles" / "udp_receiver" / "icmp_ping.log"
    ping_rows: list[dict[str, Any]] = []
    ping_stats: dict[str, Any] = {"sample_count": 0, "transmitted": 0, "received": 0, "loss_pct": None,
                                  "mean_rtt_ms": None, "median_rtt_ms": None, "p95_rtt_ms": None,
                                  "p99_rtt_ms": None, "min_rtt_ms": None, "max_rtt_ms": None}
    if ping_path.exists():
        metadata = {"run_id": run_dir.name, "server": server_ip, "udp_rate": offered_rate}
        ping_rows, ping_stats = parse_ping_log(ping_path, run_dir, metadata)
        rtt_path = run_dir / "derived" / "rtt_timeseries.csv"
        rtt_path.parent.mkdir(parents=True, exist_ok=True)
        from starlive_measure.trace.TraceDataProcess import RTT_FIELDS, write_csv as trace_write_csv
        trace_write_csv(rtt_path, RTT_FIELDS, ping_rows)

    ping_meta = run_manifest.get("icmp_companion", {})
    ping_elapsed = float(ping_stats.get("elapsed_s") or duration_s)
    ping_tx = int(ping_stats.get("transmitted") or 0)
    summary = {
        "status": "complete", "run_id": run_dir.name, "trace_dir": str(run_dir), "direction": "downlink",
        "target": server_ip, "offered_rate": offered_rate,
        "offered_rate_mbps": float(offered_rate[:-1]) if offered_rate.lower().endswith("m") else None,
        "duration_s": duration_s, "icmp_enabled": "true", "rtt_sample_count": ping_stats["sample_count"],
        "ping_transmitted": ping_tx, "ping_received": int(ping_stats.get("received") or 0),
        "ping_loss_pct": ping_stats.get("loss_pct"), "mean_rtt_ms": ping_stats.get("mean_rtt_ms"),
        "median_rtt_ms": ping_stats.get("median_rtt_ms"), "p95_rtt_ms": ping_stats.get("p95_rtt_ms"),
        "p99_rtt_ms": ping_stats.get("p99_rtt_ms"), "min_rtt_ms": ping_stats.get("min_rtt_ms"),
        "max_rtt_ms": ping_stats.get("max_rtt_ms"), "capacity_interval_count": round(duration_s * 100),
        "mean_capacity_mbps": receiver_bytes * 8 / duration_s / 1_000_000,
        "weighted_capacity_mbps": matched_bytes * 8 / active_window_duration_s / 1_000_000 if active_window_duration_s else None,
        "capacity_loss_pct": 100 * lost / transmitted if transmitted else None,
        "rtt_source": "parallel_phone_icmp_ping", "capacity_source": "matched_phone_server_packet_ledgers",
        "udp_flow_latency_sample_count": len(delays), "udp_sent_count": transmitted, "udp_received_count": delivered,
        "udp_loss_pct": 100 * lost / transmitted if transmitted else None,
        "udp_flow_latency_mean_ms": statistics.mean(delays) if delays else None,
        "udp_flow_latency_median_ms": statistics.median(delays) if delays else None,
        "udp_flow_latency_p75_ms": _percentile(delays, 75), "udp_flow_latency_p95_ms": _percentile(delays, 95),
        "udp_flow_latency_p99_ms": _percentile(delays, 99), "udp_flow_latency_min_ms": min(delays),
        "udp_flow_latency_max_ms": max(delays), "udp_latency_source": latency_mode,
        "udp_clock_offset_ms": offset_ms, "udp_clock_rtt_ms": uncertainty_ms,
        "udp_clock_sync_samples": clock.get("sample_count", ""),
        "udp_arrival_first_rel_s": "", "udp_arrival_last_rel_s": "", "udp_arrival_span_s": active_span_s,
        "udp_arrival_interval_mean_ms": "", "udp_arrival_interval_median_ms": "", "udp_arrival_interval_p95_ms": "",
        "latency_mode": latency_mode, "latency_baseline_p1_ms": baseline_ms,
        "server_unique_packets": transmitted, "phone_unique_packets": len(received), "matched_packets": delivered,
        "sender_duplicate_sequences": sent_duplicates, "receiver_duplicate_sequences": received_duplicates,
        "unmatched_sender_sequences": lost, "receiver_orphan_sequences": len(received.keys() - sent.keys()),
        "server_target_packets": server_manifest["target_packets"], "phone_target_packets": phone_manifest["target_packets"],
        "server_capture_drops": server_manifest["tcpdump"]["packets_dropped_by_kernel"],
        "phone_capture_drops": phone_manifest["tcpdump"]["packets_dropped_by_kernel"],
        "ping_actual_pps": ping_tx / ping_elapsed if ping_elapsed else None,
        "ping_process_rc": ping_meta.get("exit_code"), "phone_goodput_payload_bytes": receiver_bytes,
        "server_udp_payload_bytes": server_unique_payload_bytes,
        "server_actual_rate_mbps": server_unique_payload_bytes * 8 / duration_s / 1_000_000,
        "server_decoder_failures": decode_failure_summary(server_dir, server_manifest),
        "phone_decoder_failures": decode_failure_summary(phone_dir, phone_manifest),
        "goodput_denominator_s": duration_s, "legacy_active_window_duration_s": active_window_duration_s,
    }
    expected_mbps = summary["offered_rate_mbps"]
    invalid_reasons = []
    for label, item in (("server", server_manifest), ("phone", phone_manifest)):
        if item["tcpdump"]["packets_dropped_by_kernel"]:
            invalid_reasons.append(f"{label}_tcpdump_kernel_drops={item['tcpdump']['packets_dropped_by_kernel']}")
    if ping_meta.get("exit_code") != 0:
        invalid_reasons.append(f"ping_exit_code={ping_meta.get('exit_code')}")
    if summary["ping_actual_pps"] is None or not 80 < summary["ping_actual_pps"] <= 110:
        invalid_reasons.append(f"ping_actual_pps={summary['ping_actual_pps']}")
    if expected_mbps and abs(summary["server_actual_rate_mbps"] - expected_mbps) / expected_mbps > 0.10:
        invalid_reasons.append(f"server_actual_rate_mbps={summary['server_actual_rate_mbps']:.3f}")
    summary["analysis_valid"] = not invalid_reasons
    summary["invalid_reasons"] = invalid_reasons
    if invalid_reasons:
        summary["status"] = "invalid"
    write_csv(run_dir / "derived" / "reference_flow_summary.csv", tuple(summary.keys()), [summary])
    (run_dir / "derived" / "reference_flow_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def select_sweep_rows(rows: list[dict[str, Any]], selected_run_ids: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Return all attempts by default, or exactly the selected attempt per rate."""
    chosen = [row for row in rows if selected_run_ids is None or
              selected_run_ids.get(str(row.get("offered_rate", ""))) == row.get("run_id")]
    return sorted(chosen, key=lambda row: float(row.get("offered_rate_mbps") or 0))


def aggregate_sweep(session_dir: Path, *, relative_latency: bool = True,
                    selected_run_ids: dict[str, str] | None = None) -> Path:
    """Write the common trace summary and four PNG/PDF cross-rate figures."""
    attempts: list[dict[str, Any]] = []
    for path in sorted(session_dir.glob("root_udp_downlink_capacity_*/derived/reference_flow_summary.json")):
        attempts.append(json.loads(path.read_text(encoding="utf-8")))
    rows = select_sweep_rows(attempts, selected_run_ids)
    summary_path = session_dir / "trace_summary.csv"
    extra_fields = ("analysis_valid", "invalid_reasons", "latency_mode", "latency_baseline_p1_ms",
                    "ping_actual_pps", "server_actual_rate_mbps", "goodput_denominator_s",
                    "server_unique_packets", "phone_unique_packets", "matched_packets",
                    "phone_capture_drops", "server_capture_drops")
    summary_fields = tuple(dict.fromkeys((*SUMMARY_FIELDS, *extra_fields)))
    csv_rows = [{**row, "invalid_reasons": ";".join(row.get("invalid_reasons", []))} for row in rows]
    write_csv(summary_path, summary_fields, csv_rows)
    if not rows:
        return summary_path
    from starlive_measure.trace.TraceVisual import (
        load_matplotlib, plot_loss_rate_vs_rate, plot_mean_capacity_vs_rate,
        plot_rtt_vs_rate, plot_udp_latency_vs_rate,
    )
    plot_rows = [row for row in rows if row.get("status") == "complete"]
    if not plot_rows:
        return summary_path
    plt = load_matplotlib()
    plots = session_dir / "plots"
    plots.mkdir(parents=True, exist_ok=True)
    plot_mean_capacity_vs_rate(plots, plot_rows, plt, output_base=plots / "MeanCapacity-OfferedRate",
                               title="Phone UDP goodput vs offered rate (full 30 s)",
                               series_label="phone goodput, full-duration denominator", pdf=True)
    plot_udp_latency_vs_rate(plots, plot_rows, plt, output_base=plots / ("Latency-OfferedRate_relative" if relative_latency else "Latency-OfferedRate"),
                             title="UDP flow latency vs offered rate" + (" (relative, per-rate p1 baseline)" if relative_latency else " (clock-corrected absolute)"),
                             y_label="Relative one-way flow latency (ms)" if relative_latency else "One-way flow latency (ms)", pdf=True)
    plot_rtt_vs_rate(plots, plot_rows, plt, output_base=plots / "RTT-OfferedRate",
                     title="Parallel phone-server ICMP RTT vs offered UDP rate", pdf=True)
    plot_loss_rate_vs_rate(plots, plot_rows, plt, output_base=plots / "LossRate-OfferedRate",
                           title="UDP capacity loss and ICMP ping loss vs offered rate", pdf=True)
    return summary_path
