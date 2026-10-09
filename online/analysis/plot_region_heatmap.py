#!/usr/bin/env python3
"""Draw a country-level world heatmap from ``region_distribution.csv``.

The input contains one row per country/first-level administrative-region pair,
so rows are aggregated by ``country_code`` before they are joined to the
Natural Earth country boundaries.  The default value is unique client IPs,
which is less biased by long-running streams than the raw row count.

The boundary GeoJSON is downloaded on first use and cached outside the output
directory.  This keeps the plot reproducible without requiring geopandas or a
large checked-in map asset.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import ssl
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


WORLD_GEOJSON_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_110m_admin_0_countries.geojson"
)
DEFAULT_INPUT = Path(__file__).with_name("region_distribution.csv")
DEFAULT_OUTPUT = Path(__file__).with_name("region_heatmap_unique_ips.png")
DEFAULT_CACHE = Path(__file__).with_name(".natural_earth") / "ne_110m_admin_0_countries.geojson"
METRICS = ("rows", "unique_client_ips", "unique_streams")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--metric", choices=METRICS, default="unique_client_ips")
    parser.add_argument(
        "--geojson",
        type=Path,
        default=DEFAULT_CACHE,
        help="Natural Earth country GeoJSON; downloaded and cached when absent",
    )
    parser.add_argument(
        "--title",
        help="plot title; defaults to a title derived from --metric",
    )
    return parser.parse_args()


def load_distribution(path: Path, metric: str) -> tuple[dict[str, float], dict[str, str]]:
    values: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"country", "country_code", metric}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path} is missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            code = (row.get("country_code") or "").strip().upper()
            if not code:
                continue
            try:
                value = float(row[metric])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value) or value < 0:
                continue
            values[code] += value
            names.setdefault(code, row.get("country", code))
    return dict(values), names


def download_geojson(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        WORLD_GEOJSON_URL,
        headers={"User-Agent": "starlink-measure-region-heatmap/1.0"},
    )
    try:
        with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=30) as response:
            payload = response.read()
    except Exception as exc:  # pragma: no cover - network-dependent fallback
        raise RuntimeError(
            f"could not download Natural Earth boundaries from {WORLD_GEOJSON_URL}: {exc}"
        ) from exc
    try:
        json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("downloaded map boundary data is not valid JSON") from exc
    path.write_bytes(payload)


def load_geojson(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        print(f"Downloading country boundaries to {path}", file=sys.stderr)
        download_geojson(path)
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("type") != "FeatureCollection":
        raise ValueError(f"{path} is not a GeoJSON FeatureCollection")
    return data


def geometry_rings(geometry: Mapping[str, Any]) -> Iterable[list[list[float]]]:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates", [])
    if kind == "Polygon":
        yield from coordinates
    elif kind == "MultiPolygon":
        for polygon in coordinates:
            yield from polygon


def feature_code(feature: Mapping[str, Any]) -> str:
    properties = feature.get("properties", {})
    # ISO_A2_EH is the Natural Earth field that retains codes for France,
    # Norway, and other records whose ISO_A2 value is -99.
    return str(properties.get("ISO_A2_EH") or properties.get("ISO_A2") or "").upper()


def metric_label(metric: str) -> str:
    return {
        "rows": "records",
        "unique_client_ips": "unique client IPs",
        "unique_streams": "unique streams",
    }[metric]


def draw_plot(
    values: Mapping[str, float],
    names: Mapping[str, str],
    geojson: Mapping[str, Any],
    output: Path,
    metric: str,
    title: str | None,
) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.colors as colors
        import matplotlib.pyplot as plt
        import numpy as np
        from matplotlib.cm import ScalarMappable
        from matplotlib.collections import PatchCollection
        from matplotlib.patches import Polygon as PolygonPatch
    except ImportError as exc:
        raise RuntimeError(
            "matplotlib is required; install the project dependencies with "
            "`python3 -m pip install -e .`"
        ) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    positive = [value for value in values.values() if value > 0]
    vmax = max(positive, default=1.0)
    # Log normalization keeps small countries visible while preserving the
    # large spread between the most and least represented countries.
    norm = colors.LogNorm(vmin=1, vmax=max(1.0, vmax))
    cmap = plt.get_cmap("YlOrRd")
    background = "#eef2f5"
    edge = "#8b98a5"
    patches = []
    patch_values = []
    matched: set[str] = set()
    for feature in geojson["features"]:
        code = feature_code(feature)
        geometry = feature.get("geometry") or {}
        value = values.get(code)
        for ring in geometry_rings(geometry):
            if len(ring) < 3:
                continue
            patches.append(PolygonPatch(ring, closed=True))
            patch_values.append(value if value is not None else 0)
        if value is not None:
            matched.add(code)

    figure, axis = plt.subplots(figsize=(15, 8.2), facecolor="white")
    axis.set_facecolor(background)
    collection = PatchCollection(
        patches,
        cmap=cmap,
        norm=norm,
        linewidths=0.32,
        edgecolor=edge,
    )
    collection.set_array(np.array(patch_values, dtype=float))
    axis.add_collection(collection)
    axis.set_xlim(-180, 180)
    axis.set_ylim(-60, 90)
    axis.set_aspect("equal", adjustable="box")
    axis.axis("off")

    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    colorbar = figure.colorbar(
        sm,
        ax=axis,
        orientation="horizontal",
        fraction=0.035,
        pad=0.025,
        aspect=45,
    )
    label = metric_label(metric)
    colorbar.set_label(f"{label[0].upper() + label[1:]} (log scale)")
    colorbar.outline.set_visible(False)

    plot_title = title or f"Global distribution by country ({metric_label(metric)})"
    figure.suptitle(plot_title, x=0.08, y=0.96, ha="left", fontsize=18, fontweight="bold")
    unmatched = sorted(set(values) - matched)
    figure.text(
        0.08,
        0.925,
        "Aggregated from country/first-level administrative regions; location is inferred from IP geolocation.",
        ha="left",
        va="top",
        fontsize=9.5,
        color="#52606d",
    )
    figure.text(
        0.92,
        0.925,
        f"{len(values)} countries in data; {len(unmatched)} not drawn",
        ha="right",
        va="top",
        fontsize=9,
        color="#52606d",
    )

    top = sorted(((value, code) for code, value in values.items()), reverse=True)[:10]
    top_text = "Top 10: " + "  |  ".join(
        f"{names.get(code, code)} {value:g}" for value, code in top
    )
    figure.text(
        0.08,
        0.05,
        top_text,
        ha="left",
        va="bottom",
        fontsize=8,
        color="#52606d",
    )
    figure.text(
        0.92,
        0.05,
        "Boundaries: Natural Earth 1:110m",
        ha="right",
        va="bottom",
        fontsize=8,
        color="#52606d",
    )
    figure.tight_layout(rect=(0, 0.075, 1, 0.9))
    figure.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(figure)
    return unmatched


def main() -> None:
    args = parse_args()
    values, names = load_distribution(args.input, args.metric)
    if not values:
        raise ValueError(f"no usable country values found in {args.input}")
    geojson = load_geojson(args.geojson)
    unmatched = draw_plot(values, names, geojson, args.output, args.metric, args.title)
    print(f"Wrote {args.output}")
    if unmatched:
        print("Countries without a matching Natural Earth polygon: " + ", ".join(unmatched), file=sys.stderr)


if __name__ == "__main__":
    main()
