# Demo Test Logs

Capture Android logcat for a bounded demo test window:

```bash
bash demo_test/scripts/capture_adb_logs.sh 60 demo_test
```

Runs are stored under `demo_test/logs/`; the latest run is linked as
`demo_test/logs/latest_demo_logcat`. Use `ADB_DEVICE=<serial>` when selecting
one device from multiple connected phones.

## Capture push and pull devices together

Capture two devices with an intentional lead-in and tail around the pull-side
recording window:

```bash
bash demo_test/scripts/capture_dual_adb_logs.sh \
  --push-device <push-serial> \
  --pull-device <pull-serial> \
  --duration 60 \
  --label demo_test
```

The push-device capture starts first. Five seconds later the pull-device
capture starts and records for exactly the requested duration. The pull capture
then stops, followed by the push capture five seconds later. Consequently, a
60-second pull window produces approximately 70 seconds of push-side logs.

Runs are stored as `demo_test/logs/demo_dual_logcat_*/`, with separate `push/`
and `pull/` subdirectories. The latest run is linked as
`demo_test/logs/latest_demo_dual_logcat`. Extract either side with the existing
script:

```bash
bash demo_test/scripts/extract_relevant_logcat.sh demo_test/logs/latest_demo_dual_logcat/push
bash demo_test/scripts/extract_relevant_logcat.sh demo_test/logs/latest_demo_dual_logcat/pull
```

Set `CLEAR_LOGCAT=1` to clear both devices before capture. `LOGCAT_ARGS` and
`BASE_DIR` work the same way as for the single-device capture script.

Extract lines containing `push_stream` (case-insensitive) from the latest run:

```bash
bash demo_test/scripts/extract_relevant_logcat.sh
```

Specify a run directory or override the matching expression:

```bash
bash demo_test/scripts/extract_relevant_logcat.sh demo_test/logs/latest_demo_logcat
PATTERN='push_stream|other_field' bash demo_test/scripts/extract_relevant_logcat.sh
```

## Extract push-stream metrics

Parse every `demo_logcat_*` run and write metric CSV and compressed CSV files
inside each run directory:

```bash
python3 demo_test/scripts/extract_push_stream_metrics.py
```

Process only one run or log file:

```bash
python3 demo_test/scripts/extract_push_stream_metrics.py demo_test/logs/latest_demo_logcat
```

Each run receives `push_stream_metrics.csv`, `push_stream_metrics.csv.gz`,
`push_stream_diagnostics.csv`, `push_stream_diagnostics.csv.gz`, and extraction
metadata. The main table contains complete `live_client_monitor_log` records
whose `event_key` is `push_stream`. URLs, signatures, secrets, tokens, and
session identifiers are excluded. Truncated diagnostic JSON is counted in the
metadata and is not partially reconstructed.

## Plot push-stream metrics

Generate six time-series figures for every run with an extracted metrics CSV:

```bash
python3 demo_test/scripts/plot_push_stream_metrics.py
```

Plot only one run or CSV file:

```bash
python3 demo_test/scripts/plot_push_stream_metrics.py demo_test/logs/latest_demo_logcat
```

The figures cover two bitrate series, BWE, RTT, loss rate, actual framerate,
and cumulative stall/reconnect counts. Counter resets between stream sessions
are carried forward so the plotted totals remain cumulative across the whole
run. Each figure is written as both PNG and PDF in the run directory. Plotting
requires Matplotlib and uses its non-GUI `Agg` backend.

Each run directory can contain an `offset.txt` file with one adjustable number
in seconds. The plotter draws shallow orange dashed markers at
`time = offset + 15k`; edit this file and rerun the plotter to shift the
periodic markers.
