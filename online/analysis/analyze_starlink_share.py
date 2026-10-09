#!/usr/bin/env python3
"""Compare Starlink and Overall event counts by region and UTC period."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "online" / "data"
DEFAULT_OUTPUT = ROOT / "online" / "analysis" / "starlink_share"
PAIRS = ("SG", "USEast")
REGION_CHART_TOP_N = 20

REGION_FIELDS = (
    "server_region", "metric", "region", "period_start_utc", "period_end_utc",
    "matched_periods", "starlink_count", "overall_count", "share_pct",
    "starlink_zero_inferred_periods", "overall_zero_inferred_periods",
)
DETAIL_FIELDS = (
    "server_region", "metric", "timestamp_utc", "region", "starlink_count",
    "overall_count", "share_pct", "starlink_zero_inferred",
    "overall_zero_inferred", "status",
)
TIME_FIELDS = (
    "server_region", "metric", "timestamp_utc", "starlink_count",
    "overall_count", "share_pct", "starlink_zero_inferred_regions",
    "overall_zero_inferred_regions",
)


@dataclass
class Export:
    metric: str
    regions: Tuple[str, ...]
    values: Dict[str, Dict[str, Optional[int]]]


@dataclass
class Analysis:
    server_region: str
    metric: str
    region_rows: List[dict]
    detail_rows: List[dict]
    time_rows: List[dict]
    overall_only_times: List[str]
    starlink_only_times: List[str]
    starlink_total: int
    overall_total: int


def timestamp_utc(value: str, source: Path) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{source}: invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{source}: timestamp has no UTC offset: {value!r}")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def metric_count(value: str, source: Path, when: str, region: str) -> Optional[int]:
    value = value.strip()
    if value in ("", "-"):
        return None
    try:
        count = int(value.replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"{source}: invalid count for {region} at {when}: {value!r}") from exc
    if count < 0:
        raise ValueError(f"{source}: negative count for {region} at {when}: {count}")
    return count


def load_export(source: Path) -> Export:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError(f"{source}: empty CSV") from exc
        if len(header) < 4 or header[0] != "日期" or (len(header) - 1) % 3:
            raise ValueError(f"{source}: expected 日期 followed by region metric triplets")

        regions = []
        metrics = set()
        for index in range(1, len(header), 3):
            try:
                region, metric, suffix = header[index].rsplit("-", 2)
            except ValueError as exc:
                raise ValueError(f"{source}: invalid metric header {header[index]!r}") from exc
            expected = (
                f"{region}-{metric}-指标值",
                f"{region}-{metric}-采样量",
                f"{region}-{metric}-占比",
            )
            if suffix != "指标值" or tuple(header[index:index + 3]) != expected:
                raise ValueError(f"{source}: invalid column triplet at column {index + 1}")
            regions.append(region)
            metrics.add(metric)
        if len(regions) != len(set(regions)) or len(metrics) != 1:
            raise ValueError(f"{source}: duplicate regions or mixed metrics")

        values = {}
        for line_number, row in enumerate(reader, start=2):
            if len(row) != len(header):
                raise ValueError(f"{source}:{line_number}: expected {len(header)} columns, got {len(row)}")
            if row[0] == "平均值":
                continue
            when = timestamp_utc(row[0], source)
            if when in values:
                raise ValueError(f"{source}: duplicate timestamp {when}")
            values[when] = {
                region: metric_count(row[1 + 3 * i], source, when, region)
                for i, region in enumerate(regions)
            }
    if not values:
        raise ValueError(f"{source}: no dated rows")
    return Export(metrics.pop(), tuple(regions), values)


def share_pct(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:.6f}" if denominator else ""


def display_pct(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:.2f}%" if denominator else "—"


def analyze_pair(server_region: str, overall: Export, starlink: Export) -> Analysis:
    if overall.metric != starlink.metric:
        raise ValueError(f"{server_region}: mismatched metrics: {overall.metric} vs {starlink.metric}")
    unknown_regions = set(starlink.regions) - set(overall.regions)
    if unknown_regions:
        raise ValueError(f"{server_region}: Starlink regions absent from Overall: {sorted(unknown_regions)}")

    overall_times = set(overall.values)
    starlink_times = set(starlink.values)
    matched_times = sorted(overall_times & starlink_times)
    if not matched_times:
        raise ValueError(f"{server_region}: no matching UTC periods")
    overall_only_times = sorted(overall_times - starlink_times)
    starlink_only_times = sorted(starlink_times - overall_times)

    totals = {
        region: {"starlink": 0, "overall": 0, "starlink_inferred": 0, "overall_inferred": 0}
        for region in overall.regions
    }
    detail_rows = []
    time_rows = []
    for when in matched_times:
        starlink_sum = overall_sum = starlink_inferred = overall_inferred = 0
        for region in overall.regions:
            raw_overall = overall.values[when][region]
            raw_starlink = starlink.values[when].get(region)
            inferred_overall = raw_overall is None
            inferred_starlink = raw_starlink is None
            denominator = 0 if inferred_overall else raw_overall
            numerator = 0 if inferred_starlink else raw_starlink
            if numerator > denominator:
                raise ValueError(
                    f"{server_region}: Starlink exceeds Overall for {region} at {when}: "
                    f"{numerator} > {denominator}"
                )

            totals[region]["starlink"] += numerator
            totals[region]["overall"] += denominator
            totals[region]["starlink_inferred"] += inferred_starlink
            totals[region]["overall_inferred"] += inferred_overall
            starlink_sum += numerator
            overall_sum += denominator
            starlink_inferred += inferred_starlink
            overall_inferred += inferred_overall
            detail_rows.append({
                "server_region": server_region,
                "metric": overall.metric,
                "timestamp_utc": when,
                "region": region,
                "starlink_count": numerator,
                "overall_count": denominator,
                "share_pct": share_pct(numerator, denominator),
                "starlink_zero_inferred": int(inferred_starlink),
                "overall_zero_inferred": int(inferred_overall),
                "status": "ok" if denominator else "zero_denominator",
            })
        time_rows.append({
            "server_region": server_region,
            "metric": overall.metric,
            "timestamp_utc": when,
            "starlink_count": starlink_sum,
            "overall_count": overall_sum,
            "share_pct": share_pct(starlink_sum, overall_sum),
            "starlink_zero_inferred_regions": starlink_inferred,
            "overall_zero_inferred_regions": overall_inferred,
        })

    region_rows = []
    for region in overall.regions:
        total = totals[region]
        region_rows.append({
            "server_region": server_region,
            "metric": overall.metric,
            "region": region,
            "period_start_utc": matched_times[0],
            "period_end_utc": matched_times[-1],
            "matched_periods": len(matched_times),
            "starlink_count": total["starlink"],
            "overall_count": total["overall"],
            "share_pct": share_pct(total["starlink"], total["overall"]),
            "starlink_zero_inferred_periods": total["starlink_inferred"],
            "overall_zero_inferred_periods": total["overall_inferred"],
        })
    region_rows.sort(key=lambda row: (-int(row["starlink_count"]), str(row["region"])))
    return Analysis(
        server_region=server_region,
        metric=overall.metric,
        region_rows=region_rows,
        detail_rows=detail_rows,
        time_rows=time_rows,
        overall_only_times=overall_only_times,
        starlink_only_times=starlink_only_times,
        starlink_total=sum(int(row["starlink_count"]) for row in time_rows),
        overall_total=sum(int(row["overall_count"]) for row in time_rows),
    )


def write_csv(path: Path, fields: Tuple[str, ...], rows: List[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def plot_region_share(result: Analysis, output: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    top = sorted(result.region_rows, key=lambda row: int(row["starlink_count"]), reverse=True)[:REGION_CHART_TOP_N]
    top.reverse()
    labels = [str(row["region"]) for row in top]
    counts = [int(row["starlink_count"]) for row in top]
    percentages = [float(row["share_pct"] or 0) for row in top]
    metric_label = "play volume" if result.metric == "播放总量" else "play_stop events"

    figure, (count_axis, share_axis) = plt.subplots(
        1, 2, figsize=(13, 9.5), sharey=True,
        gridspec_kw={"width_ratios": [1.25, 1]}, facecolor="white",
    )
    positions = range(len(top))
    count_axis.barh(positions, counts, color="#2878b5")
    share_axis.barh(positions, percentages, color="#e89532")
    count_axis.set_yticks(list(positions), labels)
    count_axis.set_xlabel("Starlink count")
    share_axis.set_xlabel("Starlink / Overall (%)")
    count_scale = 1_000_000 if max(counts, default=0) >= 1_000_000 else 1_000
    count_unit = "M" if count_scale == 1_000_000 else "k"
    count_axis.xaxis.set_major_formatter(
        FuncFormatter(lambda value, _position: f"{value / count_scale:.0f}{count_unit}")
    )
    count_axis.tick_params(axis="x", labelsize=9)
    count_axis.grid(axis="x", alpha=0.2)
    share_axis.grid(axis="x", alpha=0.2)
    count_axis.set_axisbelow(True)
    share_axis.set_axisbelow(True)
    maximum = max(percentages, default=0)
    share_axis.set_xlim(0, max(1, maximum * 1.23))
    for position, percentage in enumerate(percentages):
        share_axis.text(percentage + max(0.15, maximum * 0.012), position,
                        f"{percentage:.2f}%", va="center", fontsize=8)
    figure.suptitle(
        f"{result.server_region}: regions with the most Starlink {metric_label}",
        x=0.03, y=0.99, ha="left", fontsize=15, fontweight="bold",
    )
    figure.text(0.03, 0.925, f"Top {REGION_CHART_TOP_N} by Starlink count; shares use matching Overall counts", color="#52606d")
    figure.subplots_adjust(left=0.20, right=0.97, top=0.84, bottom=0.10, wspace=0.18)
    figure.savefig(output, dpi=180)
    plt.close(figure)


def plot_useast_trend(result: Analysis, output: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    rates = {
        row["timestamp_utc"]: float(row["share_pct"]) if row["share_pct"] else float("nan")
        for row in result.time_rows
    }
    times = sorted(set(rates) | set(result.overall_only_times))
    x = [datetime.fromisoformat(when.replace("Z", "+00:00")) for when in times]
    y = [rates.get(when, float("nan")) for when in times]
    figure, axis = plt.subplots(figsize=(13, 5), facecolor="white")
    axis.plot(x, y, marker="o", markersize=4, linewidth=2, color="#2878b5")
    for when in result.overall_only_times:
        missing_time = datetime.fromisoformat(when.replace("Z", "+00:00"))
        axis.axvline(missing_time, color="#b55656", linestyle="--", alpha=0.65)
    axis.set_title("USEast: Starlink share of Overall play_stop events", loc="left", fontweight="bold")
    axis.set_xlabel("UTC time (dashed lines: no Starlink row)")
    axis.set_ylabel("Starlink / Overall (%)")
    axis.set_ylim(bottom=0)
    axis.grid(alpha=0.2)
    axis.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=timezone.utc))
    figure.autofmt_xdate(rotation=35)
    figure.tight_layout()
    figure.savefig(output, dpi=180)
    plt.close(figure)


def write_report(results: List[Analysis], output: Path, input_dir: Path) -> None:
    try:
        source_directory = str(input_dir.resolve().relative_to(ROOT))
    except ValueError:
        source_directory = str(input_dir)
    lines = [
        "# Starlink 在 Overall 事件量中的占比", "",
        "从四张导出表的 `指标值` 计算。`采样量`和原表的`占比`列不参与计算；后者表示地区在单张表内部的分布。", "",
        "计算：同一地区/UTC 时段的 Starlink ÷ Overall；跨时段先分别求和，再相除。空白地区值或缺少地区列按 0 推定，并在 CSV 中标记。分母为 0 时占比留空。", "",
        "同一对表除 Starlink 筛选条件外，其余查询条件假定一致。这里的事件量不是独立拉流数或用户数。SG 与 USEast 的事件定义不同，不能直接比较两组占比高低。", "",
        "## 结果", "",
        "| 服务区域 | 指标 | 匹配时段 | UTC 时间范围 | Starlink | Overall | 占比 |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: |",
    ]
    for result in results:
        first = result.time_rows[0]["timestamp_utc"]
        last = result.time_rows[-1]["timestamp_utc"]
        lines.append(
            f"| {result.server_region} | {result.metric} | {len(result.time_rows)} | "
            f"{first} 至 {last} | {result.starlink_total:,} | {result.overall_total:,} | "
            f"{display_pct(result.starlink_total, result.overall_total)} |"
        )
    lines += ["", "## 地区", ""]
    for result in results:
        lines += [f"### {result.server_region}（{result.metric}）", "",
                  "按 Starlink 事件量排序的前 10 个地区。完整排名及零值推定次数见 `region_share.csv`。", "",
                  "| 地区 | Starlink | Overall | 占比 |", "| --- | ---: | ---: | ---: |"]
        top = sorted(result.region_rows, key=lambda row: int(row["starlink_count"]), reverse=True)[:10]
        for row in top:
            rate = f'{float(row["share_pct"]):.2f}%' if row["share_pct"] else "—"
            lines.append(
                f'| {row["region"]} | {int(row["starlink_count"]):,} | '
                f'{int(row["overall_count"]):,} | {rate} |'
            )
        lines += [""]
    lines += ["## 时间覆盖与文件", ""]
    for result in results:
        if result.overall_only_times:
            lines.append(
                f"- {result.server_region} 的 Overall 独有时段（从两边汇总中排除）："
                + "、".join(result.overall_only_times) + "。"
            )
        if result.starlink_only_times:
            lines.append(
                f"- {result.server_region} 的 Starlink 独有时段（从两边汇总中排除）："
                + "、".join(result.starlink_only_times) + "。"
            )
    lines += [
        "- `region_share.csv`：按服务区域、地区汇总；`region_time_share.csv`：每个匹配时段的地区明细；`time_share.csv`：每个匹配时段的整体占比。`share_pct` 单位是百分数（0–100）。",
        f"- `sg_region_share.png`、`useast_region_share.png`：Starlink 事件量前 {REGION_CHART_TOP_N} 地区的计数和占比；`useast_time_share.png`：USEast 的小时趋势及缺失时段。",
        "", "## 复现", "", "在仓库根目录运行：", "",
        "```bash", "uv run --no-sync python online/analysis/analyze_starlink_share.py", "```", "",
        f"源目录：`{source_directory}`；源文件：`SG-Overall.csv`、`SG-Starlink.csv`、"
        "`USEast-Overall.csv`、`USEast-Starlink.csv`。",
        "可用 `--input-dir` 和 `--output-dir` 指定其他目录。", "",
    ]
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    results = []
    for pair in PAIRS:
        overall = load_export(args.input_dir / f"{pair}-Overall.csv")
        starlink = load_export(args.input_dir / f"{pair}-Starlink.csv")
        results.append(analyze_pair(pair, overall, starlink))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "region_share.csv", REGION_FIELDS,
              [row for result in results for row in result.region_rows])
    write_csv(args.output_dir / "region_time_share.csv", DETAIL_FIELDS,
              [row for result in results for row in result.detail_rows])
    write_csv(args.output_dir / "time_share.csv", TIME_FIELDS,
              [row for result in results for row in result.time_rows])
    plot_region_share(results[0], args.output_dir / "sg_region_share.png")
    plot_region_share(results[1], args.output_dir / "useast_region_share.png")
    plot_useast_trend(results[1], args.output_dir / "useast_time_share.png")
    write_report(results, args.output_dir / "README.md", args.input_dir)
    for result in results:
        ratio = share_pct(result.starlink_total, result.overall_total)
        print(
            f"{result.server_region} ({result.metric}): {result.starlink_total:,} / "
            f"{result.overall_total:,} = {ratio + '%' if ratio else 'N/A'} "
            f"over {len(result.time_rows)} matched periods"
        )
    print(f"Wrote analysis to {args.output_dir}")


if __name__ == "__main__":
    main()
