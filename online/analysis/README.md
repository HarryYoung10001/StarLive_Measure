# Online push-stream 数据初步分析

## 数据概览

- 文件：`online/data/281475060019287-temp_page-push_stream-查询24.csv`
- 记录数：1,620,771
- 时间范围：2026-08-10 15:59:57 至 2026-08-25 15:59:59（UTC），即北京时间
  2026-08-11 00:00 至 2026-08-26 00:00
- 独立 `client_ip`：1,770；独立 `stream_name`：2,139
- `client_isp` 全部为 Starlink
- CDN 厂商（`cdn_name`）7 个；非空 CDN ISP/节点标签（`cdn_isp`）57 个

这里的“记录占比”会受到直播持续时间和采样频率影响，不等于用户占比。因此地域和
CDN 分布同时保留了记录数、独立 IP 数和独立流数，画地域图时优先使用独立 IP 或独立
流数量。

## 地域分布

原始数据没有国家、省市或经纬度字段。以下地域于 2026-09-01 使用 ip-api.com，由
`client_ip` 的公共 IP 归属地推断，1,770 个独立 IP 均解析成功。IP 归属地，特别是
Starlink 出口 IP，可能表示地面站或网络
出口而非终端的精确物理位置；适合国家/大区级统计，不应解释为精确用户定位。

按记录数排名的前十个国家：

| 国家 | 记录数 | 记录占比 | 独立 IP | 独立 IP 占比 | 独立流 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 日本 | 241,416 | 14.90% | 34 | 1.92% | 151 |
| 澳大利亚 | 134,361 | 8.29% | 89 | 5.03% | 110 |
| 斯里兰卡 | 123,141 | 7.60% | 53 | 2.99% | 90 |
| 瓦努阿图 | 116,403 | 7.18% | 93 | 5.25% | 123 |
| 美国 | 92,278 | 5.69% | 99 | 5.59% | 94 |
| 印度尼西亚 | 90,032 | 5.55% | 146 | 8.25% | 152 |
| 东帝汶 | 86,334 | 5.33% | 92 | 5.20% | 137 |
| 巴布亚新几内亚 | 76,884 | 4.74% | 110 | 6.21% | 121 |
| 马来西亚 | 66,590 | 4.11% | 109 | 6.16% | 135 |
| 阿根廷 | 60,356 | 3.72% | 44 | 2.49% | 49 |

按独立 IP 数量看，前五名则是印度尼西亚（146）、巴布亚新几内亚（110）、马来西亚
（109）、哈萨克斯坦（107）和美国（99）。日本只有 34 个独立 IP 却产生 14.90% 的记录，
说明记录量明显受少数长时间/高频流影响，不能单独用记录数代表地域覆盖。

完整结果：

- `country_distribution.csv`：72 个国家/地区，含记录数、独立 IP 和独立流
- `region_distribution.csv`：96 个国家-一级行政区组合
- `country_cdn_distribution.csv`：每个国家内部的 CDN 厂商构成

## CDN 分布

### CDN 厂商（`cdn_name`）

| CDN | 记录数 | 记录占比 | 独立 IP | IP 覆盖率 | 独立流 | 流覆盖率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| wangsu | 1,150,830 | 71.01% | 1,280 | 72.32% | 1,439 | 67.27% |
| tencent | 387,791 | 23.93% | 486 | 27.46% | 476 | 22.25% |
| fcdn | 33,581 | 2.07% | 104 | 5.88% | 108 | 5.05% |
| hw | 28,228 | 1.74% | 57 | 3.22% | 55 | 2.57% |
| baidu | 15,230 | 0.94% | 37 | 2.09% | 32 | 1.50% |
| ksyun | 3,112 | 0.19% | 24 | 1.36% | 23 | 1.08% |
| telecom_cdn | 1,999 | 0.12% | 7 | 0.40% | 6 | 0.28% |

每个 `stream_name` 只对应一个 CDN，因此“独立流数”可用于比记录数更稳健地表示分配占比。
不同 CDN 的 IP 覆盖率和流覆盖率之和可能超过 100%，因为同一个 IP 可产生多个、分配到
不同 CDN 的流。

`cdn_isp` 是更细一级的标签。前五项为 Meteverse Limited（40.31%）、ACE CDN
（12.39%）、CDNetworks Japan（7.52%）、6 COLLYER QUAY（6.87%）和
Meteverse Limited.（5.77%）。该字段存在同义/脏标签，例如 `Meteverse Limited` 与
`Meteverse Limited.`、`ACE` 与 `ACE CDN`，正式作图前应先建立归并规则。目前原样结果
见 `cdn_isp_distribution.csv`，66 条空值统一记为 `Unknown`。

## 建议优先绘制的曲线/图

1. 地域分布：国家分级色块地图，以及 Top-N 国家/一级行政区横向条形图；主值用独立 IP
   或独立流，记录占比作为补充。
2. CDN 分布：CDN 厂商独立流占比条形图；按日 CDN 占比堆叠面积图；国家 × CDN 热力图。
   `daily_cdn_distribution.csv` 和 `country_cdn_distribution.csv` 已为后两种图准备好数据。
3. 链路质量时间序列：按小时绘制 RTT p50/p95、BWE 中位数、丢包非零率，按 CDN 或国家
   分面。不要直接画 162 万个原始点。
4. 码率自适应曲线：`bwe`、`meta_video_bitrate`、`video_enc_bitrate`、`real_bitrate`
   同轴时间曲线；以及 BWE 对实际码率散点/hexbin 图。
5. 卡顿与质量关联：RTT/BWE/丢包与 `rtmp_buffer_time` 的关系曲线，并按分辨率、接入方式、
   OS、App 分组。
6. 分布曲线：RTT、BWE、实际码率的 CDF/CCDF；按 CDN 对比时建议统一国家或对国家加权，
   避免把地域差异误判为 CDN 差异。
7. 单流过程：选择代表性 `stream_name`，画 RTT、BWE、码率、buffer 的多面板时序图，观察
   网络变化到降码率/卡顿的滞后。

## time-RTT / time-bandwidth 的粒度判断

可以直接追踪。对同一 `stream_name` 按 `timestamp` 排序后，相邻样本间隔统计为：

- 中位数 5.002 秒；p95 5.048 秒；99.40% 的正向间隔落在 4.5--5.5 秒内。
- 0.107% 是重复时间戳；0.166% 的间隔超过 10 秒，0.026% 超过 60 秒，主要应视为采样
  缺口或上报中断。
- 单流持续时间中位数约 47 分钟，85.97% 的流至少持续 1 分钟，适合做单流时序；但有少量
  只有 1--2 个样本的短流，不适合画曲线。

因此：

- `timestamp - rtt` 可以画 5 秒采样的 RTT 折线，建议同时画滚动 p50/p95；不要把相邻点
  之间的线段解释成真实连续 RTT。
- `timestamp - bwe` 也可以画，适合观察带宽估计和码率自适应趋势；跨流汇总时建议按 1 分钟
  窗口取中位数/分位数，再按 CDN、国家或接入方式分组。
- 该数据无法观察 5 秒窗口以内的亚秒级抖动、瞬时丢包或快速 Starlink 切换。若需要这类
  细节，需在采集端提高上报频率，或使用已有的 UDP/pcap/iperf 时间戳数据。

在这份数据中，412/2,139 条流出现过多个 `client_ip`，所以单流图最好标记 IP 切换点；
`cdn_name` 在每条流内保持唯一，可直接按 CDN 分面。`bwe` 的数值形态看起来像 kbps
（例如 3428 约为 3.4 Mbps），但导出字段未声明单位；正式作图前需确认单位，若转换为 Mbps
 应除以 1000，并在坐标轴标明。

## 数据清洗注意项

- RTT 有 38,517 个零值，最大 33,131；需确认零是否代表缺测，展示时宜用中位数/分位数。
- BWE 有 140,840 个零值，p99 已达 2,026,275，极端长尾明显；建议采用对数轴并做合理
  范围过滤。
- `loss_rate` 在 Windows 的 114,727 条记录中为空，绝大多数非空值为 0，最大值 1.5967；
  需先确认单位和大于 1 的语义。
- `rtmp_buffer_time` 有 117 个负数且存在数量级异常，不能直接使用均值，应先清洗后画
  p50/p95 或“发生 buffer 的记录比例”。
- 数据只有约 15 天，适合展示日内/逐日变化，不足以推断长期季节性。

## 输出和复现

运行聚合脚本：

```bash
python3 online/analysis/analyze_push_stream.py \
  online/data/281475060019287-temp_page-push_stream-查询24.csv \
  --geo-cache <private-ip-geolocation-cache.json>
```

脚本仅依赖 Python 标准库并流式读取 CSV。IP 地理解析缓存包含敏感原始 IP，不放入本目录；
对外分享时只使用本目录中的聚合 CSV。

从 `region_distribution.csv` 绘制国家级世界热力图：

```bash
uv sync --no-install-project
uv run --no-sync python online/analysis/plot_region_heatmap.py
```

默认以独立客户端 IP 数着色并输出 `region_heatmap_unique_ips.png`；可通过
`--metric rows` 或 `--metric unique_streams` 切换指标。脚本首次运行时会下载并缓存
Natural Earth 1:110m 国家边界。由于底图是国家级，CSV 中同一国家的一级行政区会先按
`country_code` 聚合；小岛国家在低分辨率底图中可能没有可着色的面。

按地区绘制气泡图（默认用独立流数表示推流需求）：

```bash
uv run --no-sync python online/analysis/plot_region_bubbles.py
```

输出为 `region_bubbles_unique_streams.png` 和对应的
`region_bubbles_unique_streams.csv`。气泡面积和颜色都按 `unique_streams` 映射；点位
优先使用 Natural Earth 一级行政区代表坐标，宽泛或无法直接匹配的地域使用脚本中的
明确 fallback，并在点位 CSV 的 `location_method/location_note` 列记录。

Tokyo 原始推流记录：

```bash
uv run --no-sync python online/analysis/extract_tokyo_push_streams.py
```

输出为 `online/data/tokyo_push_streams.csv`。脚本按 `client_ip` 的公共 IP 归属地筛选
`countryCode=JP` 且 `regionName=Tokyo`，IP 查询结果缓存在
`online/analysis/.ip_geolocation_cache.json`，可中断后续跑。输出保留原始字段，并追加
`geo_country`、`geo_country_code`、`geo_region`、`geo_city`。

将全时段 `timestamp-rtt` 折叠到 Unix/UTC 对齐的 15 秒周期：

```bash
uv run --no-sync python online/analysis/plot_time_rtt_folded_15s.py
```

周期位置计算为 `(timestamp_ms % 15000) / 1000`，保留全部有效采样点，不做分箱聚合。
输出 `time_rtt_folded_15s.png` 和 `time_rtt_folded_15s.csv`；缺失/非法值不绘制，RTT=0
保留并在图中单独统计。

将全量 `timestamp-loss_rate` 样本折叠到 Unix/UTC 对齐的 15 秒周期：

```bash
python3 online/analysis/plot_time_lossrate_folded_15s.py
```

输出 `time_lossrate_folded_15s.png` 和 `time_lossrate_folded_15s.csv`。折叠位置同样使用
`(timestamp_ms % 15000) / 1000`；绘图保留原始 `loss_rate` 比例值（不乘以 100），
缺失/非法值跳过，`loss_rate=0` 保留并在图中单独统计。

## SG 与 USEast 的 Starlink 事件量占比

四张地区导出表分别为 `online/data/SG-{Overall,Starlink}.csv` 和
`online/data/USEast-{Overall,Starlink}.csv`。运行：

```bash
uv run --no-sync python online/analysis/analyze_starlink_share.py
```

结果、地区图、USEast 小时趋势图和完整口径说明见
[`starlink_share/README.md`](starlink_share/README.md)。脚本支持 `--input-dir` 和
`--output-dir`，不会修改源表。SG 使用“播放总量”，USEast 使用“play_stop总量”；
两组指标含义不同，结果分别解释。
