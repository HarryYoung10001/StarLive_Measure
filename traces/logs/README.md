# Trace Log Index

This index is maintained in UTC. Beijing time is UTC+8. Runtime measurement
data remains ignored by Git; this README is the only tracked file in this
directory. Each row records what was measured and where the result belongs.

## Current Sessions

| Session | Measurement time (UTC / Beijing) | Workload | Devices / server | Status and purpose |
| --- | --- | --- | --- | --- |
| `dual_icmp_heavy_udp_20260805T105353Z_40M_5s` | 2026-08-05 10:53 / 18:53 | 5 s downlink, 40M, 1200 B, ICMP every 0.2 s | Dual Android phones, UDP server `<server-ip>:5201` | Initial smoke, complete |
| `dual_icmp_heavy_udp_20260805T105606Z_40M_150s` through `dual_icmp_heavy_udp_20260805T114813Z_320M_150s` | 2026-08-05 10:56-11:51 / 18:56-19:51 | Downlink sweep, 40M-320M, 150 s, 1200 B, concurrent ICMP | Dual Android phones, UDP server `<server-ip>:5201` | Heavy UDP versus ICMP comparison; complete runs, source directories archived and removed from this directory |
| `role_swap_downlink_20260805T140232Z` | 2026-08-05 14:02 onward / 22:02 onward | Downlink sweep, 100M-400M, 150 s, 1200 B, ICMP | Role-swapped dual phones, `<server-ip>:5201` | Role-swap experiment; complete |
| `role_swap_uplink_20260805T150309Z` | 2026-08-05 15:03 onward / 23:03 onward | Uplink sweep, 10M-120M, 150 s, 1200 B, ICMP | Role-swapped dual phones, `<server-ip>:5201` | Role-swap experiment; complete |
| `custom_udp_latency_downlink_20260805T203753Z` | 2026-08-05 20:38-2026-08-06 / 2026-08-06 04:38 onward | Timestamped UDP downlink sweep, 30 s, 1200 B, 200M-590M and 100M-190M | Dual phones, `<server-ip>:5201` | Capacity sweep; complete/partial runs retained |
| `64149_udp_arrival_20260808` | 2026-08-07 18:28-18:37 / 2026-08-08 02:28-02:37 | UDP uplink arrival, 100M, 1472 B, 150 s, 100 ms bandwidth window | UDP phone tunnel `64149`, no ICMP; `<server-ip>:5201` | 2 complete runs |
| `64149_udp_arrival_100ms_20260808` | 2026-08-07 18:50-19:48 / 2026-08-08 02:50-03:48 | UDP uplink arrival, 100M, 1472 B, 150 s, 100 ms bandwidth window | UDP phone tunnel `64149`, no ICMP; `<server-ip>:5201` | 13 complete runs; compact ZIP retained |
| `64149_udp_latency_10ms_20260808` | 2026-08-07 20:37-22:11 / 2026-08-08 04:37-06:11 | UDP uplink latency, 333.333333333 pps, 1472 B, 150 s, 10 ms delay window | UDP phone tunnel `64149`, no ICMP; `<server-ip>:5201` | 15 complete and 4 failed attempts; failed local run directories removed |
| `65390_49227_udp_icmp_arrival_100ms_20260808` | 2026-08-08 06:29-06:39 / 14:29-14:39 | UDP uplink arrival, 100M, 1472 B, 150 s, 100 ms; 750 ICMP pings | UDP `65390`, ICMP `49227`; `<server-ip>:5201` | Failed diagnostic runs retained for zero-bandwidth analysis |
| `smoke_65390_49227_arrival_icmp_best_effort_20260808`, `smoke_65390_49227_latency_icmp_10ms_retry_20260808` | 2026-08-08 06:24-06:27 / 14:24-14:27 | 10 s dual-phone smoke tests for arrival/latency and ICMP | UDP `65390`, ICMP `49227` | Smoke evidence retained |
| `65390_49227_udp_icmp_progress_smoke_arrival_20260808` and `_v2`, `_v3` | 2026-08-08 07:38-07:56 / 15:38-15:56 | 10 s 100M arrival smoke with sender/receiver heartbeat and monitor | UDP `65390`, ICMP `49227` | Progress telemetry validation; smoke evidence retained |
| `65390_49227_udp_icmp_progress_smoke_latency_20260808` | 2026-08-08 07:45 / 15:45 | 10 s latency smoke with 10 ms delay window and progress telemetry | UDP `65390`, ICMP `49227` | Progress telemetry validation; smoke evidence retained |
| `smoke_61332_61241_udp_icmp_arrival_100ms_20260810` | 2026-08-10 02:42-02:47 / 10:42-10:47 | Planned 10 s bandwidth smoke, 100M, 1472 B, 100 ms; 50 ICMP pings | UDP `61332`, ICMP `61241`; `<server-ip>:5201` | Preflight failed because both ADB tunnels disappeared; retained as a failed smoke record |
| `61332_61241_udp_icmp_arrival_100ms_20260810` | 2026-08-10 / 2026-08-10 | Planned: UDP uplink arrival, 100M, 1472 B, 150 s, 100 ms; 750 ICMP pings | UDP `61332`, ICMP `61241`; `<server-ip>:5201` | Not started because ADB became unavailable; no formal result |
| `smoke_53554_53610_udp_icmp_arrival_100ms_20260810` | 2026-08-10 12:46-12:51 / 20:46-20:51 | 10 s UDP uplink arrival smoke, 30M, 1472 B, 100 ms; 50 ICMP pings | UDP `53554`, ICMP `53610`; `<server-ip>:5201` | Complete; sender 29.99 Mbps, ICMP 50/50, trailing 5.96 s receive gap retained and plotted through 10 s |
| `53554_53610_udp_icmp_arrival_100ms_20260810` | 2026-08-10 13:03 onward / 21:03 onward | UDP uplink arrival, 30M, 1472 B, 150 s, 100 ms; 750 ICMP pings per run | UDP `53554`, ICMP `53610`; `<server-ip>:5201` | Running in detached `screen`; target 5 complete runs, maximum 30 attempts; failed attempts retained |

## Archives and Summaries

| Artifact | Contents |
| --- | --- |
| `dual_icmp_heavy_udp_experiment_20260805.zip` | Archive of the nine top-level `dual_icmp_heavy_udp_*` runs from the 2026-08-05 dual-phone ICMP/heavy-UDP experiment, under the outer `dual_icmp_heavy_udp_experiment_20260805/` session directory. Source quarantine: `/tmp/starlink_measure_cleanup_20260810/archived_sources/dual_icmp_heavy_udp_experiment_20260805/`. |
| `custom_udp_latency_downlink_20260805T203753Z.zip` | Existing custom UDP downlink sweep archive. |
| `64149_udp_arrival_100ms_15runs_20260808.zip` | Existing compact archive for the 64149 arrival group. |
| `64149_udp_latency_10ms_15runs_20260808.zip` | Existing compact archive for the 64149 latency group. |
| `dual_scan_complete_summary.tsv`, `trace_summary.csv` | Aggregated scan summaries from earlier experiments. |
| `capacity_mean_vs_rate.png`, `loss_rate_vs_rate.png`, `rtt_vs_rate.png` | Earlier sweep plots. `latest_dual_icmp_heavy_udp` points to the latest heavy-UDP run. |

## Maintenance Rules

- Add a row when a new top-level session, archive, summary, or plot is created.
- Record the directory timestamp in UTC and Beijing time, actual workload, device roles, server, and completion state.
- Keep smoke and diagnostic sessions explicitly labeled; do not count them as formal successful runs.
- When a failed run is removed, retain its `repeat_status.tsv` record and note the cleanup in the session row.
- New bandwidth results must be added to the current-session row after acceptance, including successful count and archive status.
- When source logs are archived and removed from `traces/logs`, keep the archive in this directory and record the recoverable quarantine path outside the repository if cleanup was safety-limited.
