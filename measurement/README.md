# Measurement

`measurement` mirrors the trace layout while preserving nanosecond source
timestamps for analysis. The runtime implementation is shared with
`traces/scripts/trace_runner.py`.

## Root phones

Root-capable phones use the parallel `traceroot.py` entry point. It executes
the phone workload and `tcpdump` through `adb shell su -c`, while the server
side helper captures a second pcap. Root profiles are stored in
`traces/profiles/root/` and write to `measurement/logs` by default:

```bash
PYTHONPATH=src python3 measurement/scripts/traceroot.py run \
  --profile traces/profiles/root/root_udp_uplink_capacity.json \
  --server-ip <server-ip> --server-ssh sgp-server \
  --role udp_sender=<root-phone-serial>

PYTHONPATH=src python3 measurement/scripts/traceroot.py sweep \
  --profile traces/profiles/root/root_udp_downlink_capacity.json \
  --server-ip <server-ip> --rates 100M,200M,500M \
  --role udp_receiver=<root-phone-serial>
```

Run with `--dry-run` first. Preflight requires `id -u` to return `0`, an
executable Android `tcpdump`, and an executable Android `iperf3` for iperf
profiles. Schema v3 root UDP profiles install the Android Go ledger extractor
before the run. Once the workload, phone tcpdump, and server helper have all
exited, the phone extracts ordered packet fields from its pcap and compresses
them into independent 32 MiB gzip chunks. The local runner pulls each chunk
with a 120-second timeout and three attempts, verifies chunk and full-ledger
SHA-256 hashes, then streams the existing `derived` outputs. It deletes the
phone pcap only after verified derivation and server artifact collection.
Server pcap retention stays remote. No extraction or compression runs during
the measurement interval.
The v3 runner collects only the server JSON, exit markers and capture summary;
server per-packet arrival text and bandwidth traces remain in its remote
`derived/` directory. Local bandwidth curves always use the phone ledger.

For a dual-ended downlink sweep, `--icmp-pps N` runs ICMP concurrently on the
same rooted phone, while `--server-ledger` installs the matching Linux extractor
on the server before the run. Both extractors start only after iperf3, ping, and
both tcpdump processes exit. The server's compact ledger is hash-verified and
transferred in chunks; the original server pcap remains remote. UDP sequence
matching reports sender-to-phone capacity loss separately from ICMP ping loss.
Full-duration phone goodput uses the requested run duration as its denominator;
the active-10-ms-window average is retained separately for comparison. When
clock uncertainty exceeds 5 ms, flow latency is labeled relative and each rate's
p1 is subtracted. The sweep writes `trace_summary.csv` and PNG/PDF rate plots.

Example reference sweep (60–300 Mbps in 10 Mbps steps, 30 seconds per rate):

```bash
PYTHONPATH=src python3 measurement/scripts/traceroot.py sweep \
  --profile traces/profiles/root/root_udp_downlink_capacity.json \
  --server-ip <server-ip> --server-ssh sgp-server --server-repo <remote-repo> \
  --role udp_receiver=<root-phone-serial> \
  --rates 60M,70M,80M,90M,100M,110M,120M,130M,140M,150M,160M,170M,180M,190M,200M,210M,220M,230M,240M,250M,260M,270M,280M,290M,300M \
  --duration 30 --interval 0.2 --payload 1200 --icmp-pps 100 --server-ledger
```

If a transfer is interrupted or the phone reconnects at a new ADB port, run:

```bash
PYTHONPATH=src python3 measurement/scripts/traceroot.py recover \
  --run-dir measurement/logs/<session>/<run> \
  --role udp_receiver=<new-adb-serial>
```

`recover` resumes the same run without generating traffic. It reuses verified
local chunks, fetches only missing or corrupt chunks, and retries pcap cleanup.
The original pcap remains on the phone until all checks succeed. Historical
schema v2 runs and their local pcap files remain readable.

Root pcap bandwidth is transport-level evidence. Packet loss and one-way
latency are marked unavailable unless packets can be associated and the clock
calibration uncertainty is below the profile threshold. RTT profiles use the
phone probe timestamps and retain pcap packet counts for cross-checking.

## Collect

```bash
python3 measurement/scripts/runner.py run --mode measurement \
  --profile traces/profiles/udp_downlink_latency_bandwidth.json \
  --server-ip <server-ip> --role udp_receiver=<adb-serial> --no-icmp
```

Use `--mode trace` for the legacy integer-millisecond artifacts. Unless
`--logs-dir` is supplied, measurement runs are written under
`measurement/logs` and trace runs under `traces/logs`.

Measurement UDP runs include `derived/udp_arrival_time_precise.tsv`,
`udp_latency_precise.tsv`, and `delay_precise.tsv`. Each file retains the
nanosecond values from UTRC and emits decimal seconds/milliseconds without
rounding. Legacy `udp_arrival_time_ms.txt` and `delay.txt` are also kept for
compatibility.

## Analysis

```bash
uv run python measurement/scripts/detect_config_phase.py measurement/logs \
  --output measurement/configuration_phase.json
uv run python measurement/scripts/plot_measurement_series.py measurement/logs
uv run python measurement/scripts/plot_measurement_series.py measurement/logs \
  --phase measurement/configuration_phase.json

# To generate a longer phase-aligned view, keep the default 102 s behavior
# and select an explicit endpoint:
uv run python measurement/scripts/plot_measurement_series.py measurement/logs \
  --phase measurement/configuration_phase.json --aligned-end-s 192
```

Each complete UDP run always receives raw latency/loss TSV and PDF outputs.
`configuration_phase.json` uses schema v2: `runs[run_id].metrics` stores an
independent phase for bandwidth, latency, and loss. Reliable metric phases
additionally produce that metric's aligned outputs under `derived/aligned`;
the first complete phase event `b+15s` is shifted to 12s (so `b+105s`
lands at 102s by default), and the result is clipped to the requested
aligned endpoint. A failed
metric phase skips only that metric's aligned files.
Plots use the reference 4:3 page layout without titles or legends.

## Loss semantics

Loss TSV values remain 0-1 ratios (plots label percentages). Sources are
`exact_total_zero`, `inferred_seq_gap`/`inferred_seq_gap_partial`, or
`unattributed_loss`; unknown bins are blank rather than fabricated lines.
Metadata records total, attributed, and unattributed packet counts.

The APKs remain in `traces/bin`; measurement does not duplicate them.

## UDP echo RTT

For an independent phone-local RTT run, use the canonical profile in
`traces/profiles/udp_echo_rtt.json`:

```bash
uv run python measurement/scripts/runner.py run --mode measurement \
  --profile traces/profiles/udp_echo_rtt.json \
  --server-ip <server-ip> --role udp_echo_phone=<adb-serial>
```

The phone sends FRTT probes, parses only the fixed header on echoed packets,
and computes RTT locally. The server uses the same compact UDP receive
strategy as the trace receiver: a large socket buffer, `MSG_TRUNC`, and
`SO_RXQ_OVFL`; payload data is not copied or processed. RTT samples are
written to `derived/rtt_10ms.tsv` and losses are bucketed by phone send time
in `derived/loss_100ms.tsv`. Kernel socket overflow is recorded separately
and invalidates the run rather than being reported as network loss.
