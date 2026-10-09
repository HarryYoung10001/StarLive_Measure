# Traces 实验入口

`traces` 是仓库中新实验的唯一编排入口。实验分为三类：

- `iperf3`：downlink 只使用手机 receiver JSON 观察带宽；uplink 由服务器同步管理 `iperf3 receiver + tcpdump`。
- `udp_flow`：使用应用层 UWLP/DWLP 时间戳协议，生成 arrival/bandwidth 或 one-way latency 轨迹。
- `icmp`：可作为主 workload，也可作为任意 iperf/UDP profile 的 companion。

已取得 root 权限的手机使用 `measurement/scripts/traceroot.py` 和
`traces/profiles/root/`，不会改变现有 v1 `trace_runner.py` 的行为。root
runner 通过 `adb shell su -c` 启动 Android `iperf3`/`tcpdump`。schema v3 的 root
UDP 实验在负载和两端抓包都退出后，才在手机上提取逐包字段、压缩为独立 gzip 块并校验回传；
服务器 pcap 保留在远端，仅同步摘要和派生文件。回传中断后使用
`traceroot.py recover --run-dir RUN --role ROLE=SERIAL` 补取缺失块，不重跑流量。

## 入口

统一命令是 `traces/scripts/trace_runner.py`。profile 位于 `traces/profiles/`，设备必须显式绑定到角色：

```bash
PYTHONPATH=src python3 -m starlive_measure.trace.trace_runner run \
  --profile traces/profiles/iperf_uplink_tcpdump.json \
  --server-ip <server-ip> --server-ssh sgp-server \
  --role udp_sender=<adb-serial> --role icmp_phone=<adb-serial>
```

rate sweep 和重复实验复用同一套 `run` 生命周期：

```bash
PYTHONPATH=src python3 -m starlive_measure.trace.trace_runner sweep \
  --profile traces/profiles/iperf_downlink_bandwidth.json \
  --server-ip <server-ip> --role udp_receiver=<adb-serial> \
  --role icmp_phone=<adb-serial> --rates 100M,150M,200M --duration 150

PYTHONPATH=src python3 -m starlive_measure.trace.trace_runner repeat \
  --profile traces/profiles/udp_uplink_latency.json \
  --server-ip <server-ip> --role udp_sender=<adb-serial> \
  --role icmp_phone=<adb-serial> --successes 5 --max-attempts 8
```

30 Mbps uplink smoke test 使用同一个 repeat session，最多保留 3 次尝试：

```bash
SESSION="traces/logs/iperf_uplink_tcpdump_30M_30s_$(date -u +%Y%m%dT%H%M%SZ)"
PYTHONPATH=src python3 -m starlive_measure.trace.trace_runner repeat \
  --profile traces/profiles/iperf_uplink_tcpdump.json \
  --server-ip <server-ip> --server-ssh sgp-server \
  --server-repo /root/StarLive_Measure \
  --role udp_sender=<adb-serial> --rate 30M --duration 30 \
  --interval 0.2 --payload 1200 --port 5201 --no-icmp \
  --successes 1 --max-attempts 3 --cooldown-s 10 \
  --session-dir "$SESSION"
```

先用 `--dry-run` 检查 profile、角色和 session 结构。正式运行时必须显式传入 `--server-ip`，不能依赖默认路由猜测目标地址。

已有 run 可以单独重新后处理，不会启动任何流量：

```bash
PYTHONPATH=src python3 -m starlive_measure.trace.trace_postprocess traces/logs/<session>/<run>
```

`traces/scripts/Bandwidth100ms.py` 可以为已有 session 批量生成非重叠 100ms
平均带宽（`bandwidth_100ms.tsv` 和 PDF 折线图），不会改变现有滚动窗口 PNG。
需要保留原始时间点并生成对齐 PDF 时，使用 `measurement/` 下的统一 runner 和分析脚本。

## 输出

每次运行写入：

```text
traces/logs/<session>/<run>/
├── manifest.json       # schema、profile、角色、命令、状态和退出码
├── profile.json        # 本次实际使用的 profile 快照
├── events.jsonl        # 生命周期事件
├── roles/              # server、udp_sender/receiver、icmp_phone 原始数据
├── raw/                # 原始协议文件
└── derived/            # 后处理结果
```

uplink iperf 的原始 PCAP 保留在 `sgp-server` 的 run directory，不通过 SCP 拉回本地；
server helper 在 tcpdump 结束后直接生成 `udp_arrival_time_ms.txt`、
`bandwidth_trace.tsv` 和 PCAP 摘要，本地只拉回这些派生文件、JSON 和日志。manifest 的
`server_pcap.transfer` 为 `remote_only`，并记录远端路径和字节数。

`sweep` 和 `repeat` 在 session 根目录额外写 `session.json`、`runs.jsonl`，失败的 child run 保留 manifest 并记录失败原因。

root run 的 `manifest.json` 使用 schema v3（历史 v2 日志仍可读），记录 `root_execution`、
`capture_sources`、`evidence_level` 和 `clock_sync`。只有完成时钟校准且不确定度
满足 profile 阈值时才会写入有效 one-way latency；否则 bandwidth、packet count
和 RTT 仍可用，但 latency/loss 会明确标记为 `unavailable`，不会从 pcap 时间戳
猜测结果。

## 编写规则

1. 新实验只能新增或修改 JSON profile，不新增同类 shell runner。
2. 生命周期、设备 preflight、清理和 sweep/repeat 逻辑只能放在 `trace_runner.py`/`trace_runtime.py`。
3. workload 只负责协议行为；服务器端固定命令放在 `runtime/server/trace_server.py`。
4. 设备通过 `--role ROLE=SERIAL` 显式指定，禁止自动选择第一个 ADB 设备。
5. uplink capacity 必须使用服务器 receiver 数据；downlink capacity 必须使用手机 receiver 数据。
6. uplink iperf 的 tcpdump 和 PCAP 后处理必须由同一个 server helper 启动、停止并登记
   artifact；原始 PCAP 留在服务端，避免把大文件拉回本地。
7. ICMP 是可选 companion；禁用时记录 `icmp.enabled=false`，不生成空 ping 目录。
8. 不在运行目录生成 heredoc runner；远端脚本必须是仓库中的稳定 helper。
9. 原始数据放 `roles/`/`raw/`，派生数据放 `derived/`，运行日志不提交到 Git。
10. 所有 profile 必须支持 dry-run；新增 profile 时补充 profile 校验和 CLI 测试。
11. 复用现有 Go/Python 协议实现；只有协议或数据契约变化时才增加新的实现。
12. profile schema 变更必须递增 `schema_version`，同时更新测试和文档。

`archive/trace_scripts_legacy/` 中的旧入口仅为历史日志和旧实验保留，不再新增功能；新的
实验、sweep 和 repeat 必须使用 `trace_runner.py`。待历史日志迁移完成后，这些入口
可以整体移除，不应继续分叉出新的 runner。

## 服务器 helper

服务器端固定入口为：

```bash
PYTHONPATH=src python3 runtime/server/trace_server.py iperf-receiver ...
PYTHONPATH=src python3 runtime/server/trace_server.py udp-downlink-sender ...
PYTHONPATH=src python3 runtime/server/trace_server.py udp-uplink-receiver ...
```

Mac 侧 runner 会在启动服务前上传当前版本的 helper。`iperf-receiver --tcpdump` 会先确认 tcpdump 进程存活，再创建 `ready` 标记，之后才允许手机启动流量。

## 验证

```bash
PYTHONPYCACHEPREFIX=/tmp/starlive_pycache python3 -m py_compile \
  traces/scripts/*.py measurement/scripts/*.py runtime/server/*.py
PYTHONPATH=src python3 -m unittest discover -s traces/tests -p 'test_*.py'
bash -n runtime/android/scripts/*.sh douyin_logs/scripts/*.sh demo_test/scripts/*.sh
```
