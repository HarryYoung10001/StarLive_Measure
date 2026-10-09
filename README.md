# StarLive_Measure

StarLive 的端到端网络和直播流测量仓库。这里是新实验体系的主仓库；旧版
`Starlink_Measure` 保留用于历史脚本和旧数据复现。

## 目录

```text
traces/              # profile、trace runner 兼容入口、后处理和测试
measurement/         # 高精度时间戳、phase 对齐和 measurement 分析
douyin_logs/         # Douyin/直播 logcat 采集、提取和绘图
demo_test/           # 双手机 demo 日志和 push-stream 指标
runtime/android/     # Android probe、APK、iperf 和 logcat 公共脚本
runtime/server/      # 远端 trace server 和 UDP helper
server_receiver/logs/ # 旧 server receiver 日志数据
server_receiver/tmp/  # 旧 runner 历史归档
logs/                # 旧版根 logs 数据
online/              # online 数据分析代码和已迁移结果
mobicom/             # 迁移的论文图表和派生数据
```

运行目录中的新日志仍按各功能目录保存，并由 `.gitignore` 忽略；迁移时的历史
结果保留在仓库中。`MIGRATION_MANIFEST.tsv` 记录了迁移数据的源路径、大小和校验和。

部分 Android 手机具备 root 权限时，可使用 `measurement/scripts/traceroot.py`
运行 `traces/profiles/root/` 中的 profile。该入口通过 `adb shell su -c` 使用
手机侧 `iperf3`/`tcpdump`，同时保留服务器端 pcap；分析脚本继续读取统一的
`derived` 输出。root pcap 的 bandwidth 是传输层观测，one-way latency 只有在
时钟校准误差满足 profile 阈值时才会标记有效。

## 环境

```bash
uv sync
```

需要真实采集时，Mac 侧应准备 `adb`/Reemo，服务器侧应准备 `python3`、`iperf3`、
`tcpdump` 和 SSH。远端仓库默认是 `/root/StarLive_Measure`，也可以通过
`--server-repo` 或 `STARLIVE_SERVER_REPO` 覆盖。

## Trace 采集

所有新实验使用 JSON profile 和统一 runner：

```bash
uv run python traces/scripts/trace_runner.py run \
  --profile traces/profiles/iperf_downlink_bandwidth.json \
  --server-ip <server-ip> \
  --role udp_receiver=<adb-serial> \
  --role icmp_phone=<adb-serial>
```

批量速率和重复实验复用同一生命周期：

```bash
uv run python traces/scripts/trace_runner.py sweep \
  --profile traces/profiles/iperf_downlink_bandwidth.json \
  --server-ip <server-ip> --role udp_receiver=<adb-serial> \
  --rates 100M,150M,200M --duration 150

uv run python traces/scripts/trace_runner.py repeat \
  --profile traces/profiles/udp_uplink_latency.json \
  --server-ip <server-ip> --role udp_sender=<adb-serial> \
  --successes 5 --max-attempts 8
```

先用 `--dry-run` 检查 profile、角色、服务器和输出目录。已有 run 可单独后处理：

```bash
uv run python traces/scripts/trace_postprocess.py traces/logs/<session>/<run>
```

服务器 helper 位于 `runtime/server/`，Android probe 位于 `runtime/android/probes/`。

## Measurement 分析

`measurement` 复用同一 runner，但保留纳秒源时间戳：

```bash
uv run python measurement/scripts/runner.py run --mode measurement \
  --profile traces/profiles/udp_downlink_latency_bandwidth.json \
  --server-ip <server-ip> --role udp_receiver=<adb-serial> --no-icmp

uv run python measurement/scripts/detect_config_phase.py measurement/logs \
  --output measurement/configuration_phase.json
uv run python measurement/scripts/plot_measurement_series.py measurement/logs
```

所有 profile（包括 `udp_echo_rtt`）的唯一来源是 `traces/profiles/`；
`measurement/profiles` 只是兼容链接。

## Douyin 和 Demo 日志

单手机 logcat 捕获共用 `runtime/android/scripts/capture_adb_logs_common.sh`：

```bash
bash douyin_logs/scripts/capture_adb_logs.sh 60 douyin
bash demo_test/scripts/capture_adb_logs.sh 60 demo_test
```

之后分别运行领域专用的过滤、指标提取和绘图脚本。双手机采集使用：

```bash
bash demo_test/scripts/capture_dual_adb_logs.sh \
  --push-device <push-serial> --pull-device <pull-serial> \
  --duration 60 --label demo_test
```

## 验证

```bash
bash -n runtime/android/scripts/*.sh douyin_logs/scripts/*.sh demo_test/scripts/*.sh
PYTHONPATH=src python3 -m py_compile \
  traces/scripts/*.py measurement/scripts/*.py \
  douyin_logs/scripts/*.py demo_test/scripts/*.py runtime/server/*.py
PYTHONPATH=src python3 -m unittest discover -s traces/tests -p 'test_*.py'
PYTHONPATH=src python3 -m unittest discover -s measurement/tests -p 'test_*.py'
```

真实设备和服务器实验不属于自动化测试；正式运行前先执行 dry-run，再进行短时
10--15 秒 smoke test。
