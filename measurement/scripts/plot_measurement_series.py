#!/usr/bin/env python3
"""Generate raw latency/loss plots and optional phase-aligned series."""
from __future__ import annotations

import argparse, csv, gzip, json, math, sys
from pathlib import Path

try:
    from .common import find_runs, latency_rows, load_manifest, load_profile
    from .loss_rate import process_run
    from starlive_measure.trace.Bandwidth100ms import build_100ms_points, packet_events
except ImportError:
    from common import find_runs, latency_rows, load_manifest, load_profile
    from loss_rate import process_run
    from starlive_measure.trace.Bandwidth100ms import build_100ms_points, packet_events

from starlive_measure.measurement import aligned, aligned_ticks

def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt

def _plot(rows, output, ylabel, *, aligned=False, x_end=None, percent=False, scatter=False):
    plt = _plt(); fig, ax = plt.subplots(figsize=(4, 3))
    known = [(x, y * 100 if percent else y) for x, y in rows if y is not None]
    if known and not scatter:
        ax.plot([x for x, _ in known], [y for _, y in known], color="#2563eb", linewidth=0.8)
    elif known:
        # RTT buckets are rendered as points; do not imply interpolation across
        # empty buckets or outages with a connecting line.
        ax.scatter([x for x, _ in known], [y for _, y in known], color="#2563eb", s=4, alpha=0.7, linewidths=0)
    aligned_end = float(x_end or 102.0)
    ax.set_xlim(0, aligned_end if aligned else max(1.0, x_end or (max((x for x, _ in rows), default=0))))
    if aligned:
        ticks = aligned_ticks(aligned_end)
        ax.set_xticks(ticks)
        for tick in ticks[1:-1]:
            ax.axvline(tick, color="#9ca3af", linestyle="--", linewidth=0.4, alpha=0.7)
    ax.set_xlabel("Time (s)", fontsize=14); ax.set_ylabel(ylabel, fontsize=14)
    ax.tick_params(axis="both", labelsize=7)
    ax.grid(True, linestyle="--", linewidth=0.45, color="#9ca3af", alpha=0.7)
    ax.set_ylim(bottom=0); fig.tight_layout(pad=0.5)
    output.parent.mkdir(parents=True, exist_ok=True); fig.savefig(output, format="pdf"); plt.close(fig)

def _write_tsv(path, rows, field, aligned=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as h:
        w = csv.writer(h, delimiter="\t", lineterminator="\n"); w.writerow(["time_s" if aligned else "time_ms", field])
        for x, y in rows:
            if aligned:
                x_text = f"{x:.6f}"
                y_text = "" if y is None else f"{y:.9f}"
            else:
                x_text = repr(x)
                y_text = "" if y is None else repr(y)
            w.writerow([x_text, y_text])

def _loss_rows(run):
    path = run / "derived" / "loss_100ms.tsv"
    if not path.is_file(): process_run(run)
    with path.open(encoding="utf-8", newline="") as h:
        out=[]
        for r in csv.DictReader(h, delimiter="\t"):
            try: x=float(r["time_ms"]); y=float(r["loss_rate"]) if r.get("loss_rate") not in (None, "") else None
            except (TypeError, ValueError): continue
            out.append((x, y))
        return out

def _rtt_rows(run):
    path = run / "derived" / "rtt_10ms.tsv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as h:
            return [((float(r.get("time_s", r.get("time_ms", "0"))) if "time_s" in r else float(r["time_ms"]) / 1000.0), float(r["rtt_ms"])) for r in csv.DictReader(h, delimiter="\t") if r.get("rtt_ms") not in (None, "")]

def _oneway_rows(run, window_ms):
    # Use only per-packet latency samples.  delay_precise.tsv contains
    # interpolated empty windows and must not be used for a raw scatter.
    source = run / "derived" / "udp_latency_precise.tsv"
    if not source.is_file():
        source = run / "derived" / "delay_precise.tsv"
    if not source.is_file():
        return []
    buckets = {}
    with source.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            try:
                if "window_start_ms" in row:
                    t = float(row["window_start_ms"])
                else:
                    t = float(row["receive_time_s"]) * 1000.0
                value = float(row.get("mean_delay_ms", row.get("delay_ms", "")))
                count = int(float(row.get("sample_count", "1") or 1))
            except (TypeError, ValueError):
                continue
            index = int(math.floor(t / window_ms))
            total, samples = buckets.get(index, (0.0, 0))
            buckets[index] = (total + value * count, samples + count)
    return [(index * window_ms / 1000.0, total / samples, samples) for index, (total, samples) in sorted(buckets.items())]

def _sender_rows(run):
    candidates = [run / "roles" / "udp_sender" / "uplink_udp_sender.csv.gz", run / "roles" / "udp_sender" / "remote" / "uplink_udp_sender.csv.gz"]
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        return []
    with gzip.open(source, "rt", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))

def _received_sequences(run):
    # The latency table may legitimately be a filtered/interpolated product
    # (and in older runs it can be incomplete).  Arrival-time precise is the
    # authoritative per-packet receive record for loss accounting.
    source = run / "derived" / "udp_arrival_time_precise.tsv"
    if not source.is_file():
        source = run / "derived" / "udp_latency_precise.tsv"
    if not source.is_file():
        return set()
    with source.open(encoding="utf-8", newline="") as handle:
        out = set()
        for row in csv.DictReader(handle, delimiter="\t"):
            try:
                out.add(int(row["seq"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

def _loss_rows(run, sample_ms, window_ms, duration_s):
    sender = _sender_rows(run)
    received = _received_sequences(run)
    packets = []
    for row in sender:
        try:
            packets.append((float(row["send_rel_s"]) * 1000.0, 0.0 if int(row["seq"]) in received else 1.0))
        except (KeyError, TypeError, ValueError):
            continue
    summary = run / "roles" / "udp_sender" / "uplink_udp_sender_summary.txt"
    send_failed = 0
    if summary.is_file():
        for line in summary.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("send_failed_packets="):
                try:
                    send_failed = int(line.split("=", 1)[1])
                except ValueError:
                    pass
                break
    if not packets:
        return [], {"sent_packets": 0, "received_packets": 0, "lost_packets": 0, "send_failed_packets": send_failed}
    packets.sort()
    centers = [float(t) for t in range(0, max(0, int(math.ceil(duration_s * 1000.0))) + 1, max(1, int(sample_ms)))]
    half = window_ms / 2.0
    result = []
    for center in centers:
        left = max(0.0, center - half)
        right = min(duration_s * 1000.0, center + half)
        values = [loss for timestamp, loss in packets if left <= timestamp < right]
        result.append((center / 1000.0, sum(values) / len(values) if values else None))
    lost = int(sum(loss for _timestamp, loss in packets))
    return result, {"sent_packets": len(packets), "received_packets": len(packets) - lost, "lost_packets": lost, "send_failed_packets": send_failed}

def _write_rows(path, rows, header):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)

def _duration(run):
    try: return float(load_profile(run).get("workload", {}).get("duration_s"))
    except (TypeError, ValueError): return None

def _bandwidth_rows(run):
    path = run / "derived" / "bandwidth_100ms.tsv"
    if not path.is_file():
        points = build_100ms_points(packet_events(run), duration_ms=(_duration(run) or 0) * 1000)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as h:
            w = csv.DictWriter(h, fieldnames=["time_ms", "interval_end_ms", "packet_count", "payload_bytes", "bandwidth_mbps"], delimiter="\t"); w.writeheader(); w.writerows(points)
    with path.open(encoding="utf-8", newline="") as h:
        rows = csv.DictReader(h, delimiter="\t")
        return [
            (
                float(row["time_s"])
                if row.get("time_s") not in (None, "")
                else float(row["time_ms"]) / 1000.0,
                float(row["bandwidth_mbps"]),
            )
            for row in rows
        ]

def plot_raw(run, latency_window_ms=10.0, loss_sample_ms=50.0, loss_window_ms=200.0):
    end = _duration(run)
    is_echo = load_profile(run).get("workload", {}).get("kind") == "udp_echo_rtt"
    bandwidth = [] if is_echo else _bandwidth_rows(run)
    is_uplink = load_profile(run).get("workload", {}).get("kind") == "udp_flow" and load_profile(run).get("workload", {}).get("direction") == "uplink"
    latency = ([(x / 1000.0, y) for x, y in latency_rows(run)] if not is_uplink else [(x, y) for x, y, _count in _oneway_rows(run, latency_window_ms)])
    if is_uplink and end is not None:
        loss, loss_meta = _loss_rows(run, loss_sample_ms, loss_window_ms, end)
    else:
        loss = [(x / 1000.0, y) for x, y in _loss_rows(run, 100.0, 100.0, end or 0.0)[0]]
        loss_meta = {}
    rtt = _rtt_rows(run)
    if is_uplink:
        one_way = _oneway_rows(run, latency_window_ms)
        _write_rows(run / "derived" / "oneway_latency_10ms.tsv", [(f"{x:.6f}", f"{y:.9f}", count) for x, y, count in one_way], ["time_s", "oneway_latency_ms", "sample_count"])
    else:
        _write_tsv(run / "derived" / "latency_10ms.tsv", [(x * 1000.0, y) for x, y in latency], "delay_ms")
    if rtt:
        _write_tsv(run / "derived" / "rtt_10ms.tsv", [(x * 1000.0, y) for x, y in rtt], "rtt_ms")
    if not is_echo:
        _plot(latency, run / "derived" / ("oneway_latency_10ms_raw.pdf" if is_uplink else "latency_10ms_raw.pdf"), "One-way latency (ms)" if is_uplink else "Latency (ms)", x_end=end, scatter=False)
    _plot(loss, run / "derived" / "loss_100ms_raw.pdf", "Loss rate (%)", x_end=end, percent=True)
    if not is_echo:
        _plot(bandwidth, run / "derived" / "bandwidth_100ms_raw.pdf", "Bandwidth (Mbps)", x_end=end)
    if is_uplink and end is not None:
        loss_points = loss
        _write_rows(run / "derived" / "loss_50ms_rolling200ms.tsv", [(f"{x:.6f}", "" if y is None else f"{y:.9f}") for x, y in loss_points], ["time_s", "loss_rate"])
        write_json = run / "derived" / "loss_50ms_rolling200ms_metadata.json"
        write_json.write_text(json.dumps({"sample_interval_ms": loss_sample_ms, "window_ms": loss_window_ms, "window": "centered", "edge_policy": "clamp", **loss_meta}, indent=2) + "\n", encoding="utf-8")
        _plot(loss_points, run / "derived" / "loss_50ms_rolling200ms_raw.pdf", "Loss rate (%)", x_end=end, percent=True, scatter=True)
    _plot(rtt, run / "derived" / "rtt_10ms_raw.pdf", "RTT (ms)", x_end=end, scatter=True)

def plot_aligned(run, phase, end_s=102.0):
    end_s = float(end_s)
    if end_s <= 0:
        raise ValueError("aligned end must be positive")
    is_uplink = load_profile(run).get("workload", {}).get("kind") == "udp_flow" and load_profile(run).get("workload", {}).get("direction") == "uplink"
    duration = _duration(run) or 0.0
    metrics = {"bandwidth": _bandwidth_rows(run), "latency": ([(x, y) for x, y, _count in _oneway_rows(run, 10.0)] if is_uplink else [(x / 1000.0, y) for x, y in latency_rows(run)]), "loss": ([(x, y) for x, y in _loss_rows(run, 50.0, 200.0, duration)[0]] if is_uplink else [(x / 1000.0, y) for x, y in _loss_rows(run)]), "rtt": _rtt_rows(run)}
    fields = {"bandwidth": ("bandwidth_mbps", "Bandwidth (Mbps)", False), "latency": ("oneway_latency_ms" if is_uplink else "delay_ms", "One-way latency (ms)" if is_uplink else "Latency (ms)", False), "loss": ("loss_rate", "Loss rate (%)", True), "rtt": ("rtt_ms", "RTT (ms)", False)}
    metadata = {}
    for metric, rows in metrics.items():
        info = phase.get("metrics", {}).get(metric, {})
        if info.get("status") != "ok" or info.get("b_s") is None:
            metadata[metric] = {"status": info.get("status", "missing"), "reason": info.get("status", "missing")}; continue
        shifted = aligned(rows, info["b_s"], end_s=end_s); field, label, percent = fields[metric]
        stem = {"bandwidth": "bandwidth_100ms", "latency": "oneway_latency_10ms" if is_uplink else "latency_10ms", "loss": "loss_50ms_rolling200ms" if is_uplink else "loss_100ms", "rtt": "rtt_10ms"}[metric]
        _write_tsv(run / "derived" / "aligned" / f"{stem}.tsv", shifted, field, aligned=True)
        pdf_name = f"{stem}.pdf"
        _plot(shifted, run / "derived" / "aligned" / pdf_name, label, aligned=True, x_end=end_s, percent=percent, scatter=(metric == "rtt" or is_uplink and metric == "loss"))
        shift = 12.0 - (float(info["b_s"]) + 15.0)
        metadata[metric] = {"status": "ok", "b_s": info["b_s"], "shift_s": shift, "anchor_event_s": float(info["b_s"]) + 15.0, "target_event_s": 12.0, "end_s": end_s, "outputs": [pdf_name]}
    return metadata

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("input", type=Path); p.add_argument("--phase", type=Path)
    p.add_argument("--aligned-end-s", type=float, default=102.0, help="aligned output endpoint in seconds (default: 102)")
    p.add_argument("--latency-window-ms", type=float, default=10.0); p.add_argument("--loss-sample-ms", type=float, default=50.0); p.add_argument("--loss-window-ms", type=float, default=200.0)
    a=p.parse_args(argv)
    if a.latency_window_ms <= 0 or a.loss_sample_ms <= 0 or a.loss_window_ms <= 0 or a.aligned_end_s <= 0:
        p.error("window and sample intervals must be positive")
    runs=find_runs(a.input)
    if not runs: print(f"ERROR: no run manifests found below {a.input}", file=sys.stderr); return 2
    phase = None
    if a.phase:
        try: phase=json.loads(a.phase.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as e: print(f"ERROR: invalid phase file: {e}", file=sys.stderr); return 2
    for run in runs:
        try:
            manifest = load_manifest(run)
            workload = load_profile(run).get("workload", {}) or manifest.get("workload", {})
            if manifest.get("status") and manifest.get("status") != "complete":
                metadata = {"status": "skipped", "reason": f"run status is {manifest['status']}"}
                (run / "derived" / "plot_metadata.json").parent.mkdir(parents=True, exist_ok=True)
                (run / "derived" / "plot_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
                print(f"Skipped plots for {run}: {metadata['reason']}")
                continue
            if workload.get("kind") in {"icmp", "udp_echo_rtt"}:
                metadata = {"status": "skipped", "reason": "ICMP run has no UDP time-series inputs"}
                (run / "derived" / "plot_metadata.json").parent.mkdir(parents=True, exist_ok=True)
                (run / "derived" / "plot_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
                print(f"Skipped plots for {run}: {metadata['reason']}")
                continue
            plot_raw(run, a.latency_window_ms, a.loss_sample_ms, a.loss_window_ms); metadata={"raw": {"latency": True, "loss": True, "bandwidth": True, "rtt": bool(_rtt_rows(run))}}
            if phase is not None:
                run_phase = phase.get("runs", {}).get(run.name)
                if run_phase is None: raise ValueError("phase file has no entry for run")
                metadata["aligned"] = plot_aligned(run, run_phase, end_s=a.aligned_end_s)
            (run / "derived" / "plot_metadata.json").write_text(json.dumps(metadata, indent=2)+"\n", encoding="utf-8")
            print(f"Wrote raw plots for {run}")
        except (OSError, ValueError, ImportError) as e: print(f"ERROR: {run}: {e}", file=sys.stderr); return 1
    return 0
if __name__ == "__main__": raise SystemExit(main())
