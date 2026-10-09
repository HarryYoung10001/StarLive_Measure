# Starlink 在 Overall 事件量中的占比

从四张导出表的 `指标值` 计算。`采样量`和原表的`占比`列不参与计算；后者表示地区在单张表内部的分布。

计算：同一地区/UTC 时段的 Starlink ÷ Overall；跨时段先分别求和，再相除。空白地区值或缺少地区列按 0 推定，并在 CSV 中标记。分母为 0 时占比留空。

同一对表除 Starlink 筛选条件外，其余查询条件假定一致。这里的事件量不是独立拉流数或用户数。SG 与 USEast 的事件定义不同，不能直接比较两组占比高低。

## 结果

| 服务区域 | 指标 | 匹配时段 | UTC 时间范围 | Starlink | Overall | 占比 |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| SG | 播放总量 | 2 | 2026-10-02T00:00:00Z 至 2026-10-03T00:00:00Z | 288,998,012 | 13,517,149,567 | 2.14% |
| USEast | play_stop总量 | 23 | 2026-10-04T09:00:00Z 至 2026-10-05T08:00:00Z | 3,052,492 | 417,420,377 | 0.73% |

## 地区

### SG（播放总量）

按 Starlink 事件量排序的前 10 个地区。完整排名及零值推定次数见 `region_share.csv`。

| 地区 | Starlink | Overall | 占比 |
| --- | ---: | ---: | ---: |
| Indonesia | 79,295,086 | 2,892,959,531 | 2.74% |
| Iraq | 18,655,744 | 421,591,946 | 4.43% |
| Brazil | 13,896,008 | 840,842,173 | 1.65% |
| Sudan | 13,136,799 | 28,870,055 | 45.50% |
| Burkina Faso | 12,012,198 | 37,577,212 | 31.97% |
| Philippines | 11,703,565 | 499,268,373 | 2.34% |
| Mexico | 11,700,680 | 406,569,966 | 2.88% |
| Yemen | 10,390,388 | 19,179,764 | 54.17% |
| Peru | 9,297,476 | 422,581,577 | 2.20% |
| Ivory Coast | 8,197,777 | 98,472,035 | 8.32% |

### USEast（play_stop总量）

按 Starlink 事件量排序的前 10 个地区。完整排名及零值推定次数见 `region_share.csv`。

| 地区 | Starlink | Overall | 占比 |
| --- | ---: | ---: | ---: |
| Texas | 507,978 | 50,684,732 | 1.00% |
| California | 408,187 | 55,568,501 | 0.73% |
| Arizona | 221,123 | 8,487,211 | 2.61% |
| Florida | 214,050 | 35,036,039 | 0.61% |
| Mexico City | 189,520 | 406,454 | 46.63% |
| New Mexico | 154,243 | 2,275,971 | 6.78% |
| Virginia | 135,808 | 9,817,010 | 1.38% |
| Missouri | 120,989 | 6,555,698 | 1.85% |
| Louisiana | 116,883 | 6,165,000 | 1.90% |
| Alaska | 113,653 | 431,639 | 26.33% |

## 时间覆盖与文件

- USEast 的 Overall 独有时段（从两边汇总中排除）：2026-10-04T10:00:00Z、2026-10-05T09:00:00Z。
- `region_share.csv`：按服务区域、地区汇总；`region_time_share.csv`：每个匹配时段的地区明细；`time_share.csv`：每个匹配时段的整体占比。`share_pct` 单位是百分数（0–100）。
- `sg_region_share.png`、`useast_region_share.png`：Starlink 事件量前 20 地区的计数和占比；`useast_time_share.png`：USEast 的小时趋势及缺失时段。

## 复现

在仓库根目录运行：

```bash
uv run --no-sync python online/analysis/analyze_starlink_share.py
```

源目录：`online/data`；源文件：`SG-Overall.csv`、`SG-Starlink.csv`、`USEast-Overall.csv`、`USEast-Starlink.csv`。
可用 `--input-dir` 和 `--output-dir` 指定其他目录。
