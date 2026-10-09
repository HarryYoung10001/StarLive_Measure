# Douyin Logs

Mac-side helpers for capturing adb-visible Android/Reemo cloud phone logs while
running Douyin experiments.

## Capture Logcat

```bash
bash douyin_logs/scripts/capture_adb_logs.sh 60 douyin
```

Arguments:

```text
60       capture duration in seconds
douyin   run label used in the output directory name
```

The script writes each run under:

```text
douyin_logs/logs/douyin_logcat_<UTC_TS>_<DURATION>s_<LABEL>/
```

It also updates:

```text
douyin_logs/logs/latest_douyin_logcat
```

Useful environment variables:

```bash
ADB_DEVICE=<device-id> bash douyin_logs/scripts/capture_adb_logs.sh 60 douyin
CLEAR_LOGCAT=1 bash douyin_logs/scripts/capture_adb_logs.sh 30 clean_window
LOGCAT_ARGS='-b all -v threadtime *:I' bash douyin_logs/scripts/capture_adb_logs.sh 60 info_only
LOGCAT_SINCE='07-13 14:30:00.000' bash douyin_logs/scripts/capture_adb_logs.sh 60 manual_start
```

By default, the script does not clear the Android logcat ring buffer. It records
Mac and Android timestamps around the capture so the intended time window is
visible in the saved metadata, and starts logcat from the Android timestamp
recorded at capture start.

## Extract Relevant Lines

After capture, extract lines containing Douyin/TikTok/Aweme/media/audio related
keywords:

```bash
bash douyin_logs/scripts/extract_relevant_logcat.sh
```

By default this reads `douyin_logs/logs/latest_douyin_logcat/logcat.txt` and
writes `logcat_relevant.txt` in the same run directory.

You can also specify a run directory or a logcat file:

```bash
bash douyin_logs/scripts/extract_relevant_logcat.sh douyin_logs/logs/latest_douyin_logcat
bash douyin_logs/scripts/extract_relevant_logcat.sh douyin_logs/logs/latest_douyin_logcat/logcat.txt filtered.txt
```

Override the matching keywords with `PATTERN`:

```bash
PATTERN='douyin|aweme|audio' bash douyin_logs/scripts/extract_relevant_logcat.sh
```

## Extract Live Metrics

Convert captured live log lines into one sparse CSV:

```bash
python3 douyin_logs/scripts/extract_live_metrics.py
```

By default this reads `douyin_logs/logs/latest_douyin_logcat/logcat_relevant.txt`
and writes both plain and compressed CSV files:

```text
douyin_logs/logs/latest_douyin_logcat/live_metrics_timeseries.csv
douyin_logs/logs/latest_douyin_logcat/live_metrics_timeseries.csv.gz
```

You can also pass a run directory or a specific log file:

```bash
python3 douyin_logs/scripts/extract_live_metrics.py douyin_logs/logs/latest_douyin_logcat
python3 douyin_logs/scripts/extract_live_metrics.py douyin_logs/logs/latest_douyin_logcat/logcat_relevant.txt
```

The CSV is a sparse unified table. Use `event_type` to distinguish `sei`,
`network_quality`, `api_timing`, `websocket_traffic`, and `room_context` rows;
not every row has every metric column.

## Plot Live Metrics

After extracting the CSV, generate PNG time-series plots. Use the
`meshagent-plot` conda environment for plotting:

```bash
conda run -n meshagent-plot python douyin_logs/scripts/plot_live_metrics.py
```

The current default input is:

```text
douyin_logs/logs/douyin_logcat_20260713T120238Z_300s_douyin_watch/live_metrics_timeseries.csv
```

You can also pass a run directory or a specific CSV/CSV.GZ file:

```bash
conda run -n meshagent-plot python douyin_logs/scripts/plot_live_metrics.py douyin_logs/logs/douyin_logcat_20260713T120238Z_300s_douyin_watch
conda run -n meshagent-plot python douyin_logs/scripts/plot_live_metrics.py douyin_logs/logs/douyin_logcat_20260713T120238Z_300s_douyin_watch/live_metrics_timeseries.csv
```

The plotter writes PNG files next to the CSV. It no longer writes PDF files.
Missing metric series are skipped with warnings.

If the run directory contains `offset.txt`, the first number in that file is
read as `m` seconds. Each plot marks `time = m + 15k` with a light red dashed
vertical line.

Current plot outputs:

```text
live_bitrate_time.png               real_bitrate; actual live video bitrate
live_framerate_time.png             real_video_framerate; actual video FPS
live_ntp_diff_time.png              ntp_diff; clock/time-reference difference in ms
live_network_rtt_time.png           http_rtt, tcp_rtt, quic_rtt in ms
live_downstream_throughput_time.png downstream_throughput_kbps; estimated downstream capacity
live_video_download_speed_time.png  video_download_speed; player-reported video download speed
live_quic_loss_rate_time.png        quic_receive_loss_rate, quic_send_loss_rate
live_api_timing_time.png            timing_total, timing_waiting, timing_receive, timing_send
live_ws_bytes_time.png              sent_bytes, received_bytes for WebSocket traffic
```

Notes:

- `live_stream_delay_time.png` and `live_audio_loudness_time.png` are currently
  disabled.
- `net_effective_connection_type` remains in the CSV but is not plotted.
- `downstream_throughput_kbps` is an app/network-stack estimate and often jumps
  between discrete levels; treat sudden rises as estimator or quality-probe
  changes unless corroborated by RTT, errors, and video bitrate.
- `live_api_timing_time.png` shows API request timing, not playback bitrate or
  live latency. A visual drop to zero may simply mean no timing sample was
  present for that interval.
