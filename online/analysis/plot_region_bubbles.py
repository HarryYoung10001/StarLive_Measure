#!/usr/bin/env python3
"""Plot region-level push-stream demand as sized and colored bubbles.

``region_distribution.csv`` is geolocated at the country/administrative-region
level, not necessarily at city level.  This script places each row at a
Natural Earth admin-1 representative point when the region name matches; for
broad or unmatched names it falls back to the country representative point and
records that decision in a companion CSV.  Bubble area and color represent the
selected metric (``unique_streams`` by default).
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import re
import ssl
import sys
import unicodedata
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping


ADMIN1_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_10m_admin_1_states_provinces.geojson"
)
COUNTRIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_110m_admin_0_countries.geojson"
)
HERE = Path(__file__).resolve().parent
DEFAULT_INPUT = HERE / "region_distribution.csv"
DEFAULT_OUTPUT = HERE / "region_bubbles_unique_streams.png"
DEFAULT_POINTS = HERE / "region_bubbles_unique_streams.csv"
CACHE_DIR = HERE / ".natural_earth"
METRICS = ("rows", "unique_client_ips", "unique_streams")

# These are administrative-region labels returned by ip-api that have a
# different spelling/level in Natural Earth.  They are intentionally explicit
# so a future rerun cannot silently move a marker because of fuzzy matching.
ALIASES = {
    ("LK", "Western Province"): (6.90, 80.00, "alias: Western Province center"),
    ("CI", "Abidjan Autonomous District"): (5.35, -4.02, "alias: Abidjan"),
    ("FR", "Île-de-France"): (48.85, 2.35, "alias: Paris"),
    ("GB", "England"): (52.35, -1.50, "alias: England center"),
    ("DK", "Capital Region"): (55.85, 12.35, "alias: Copenhagen"),
    ("MX", "Mexico City"): (19.43, -99.13, "alias: Mexico City"),
    ("KI", "Gilbert Islands"): (1.45, 173.00, "alias: Gilbert Islands center"),
    ("WS", "Tuamasaga"): (-13.84, -171.77, "alias: Apia"),
    ("AR", "Buenos Aires F.D."): (-34.62, -58.45, "alias: Buenos Aires city"),
    ("KZ", "Almaty"): (43.24, 76.95, "alias: Almaty city"),
    ("PH", "National Capital Region"): (14.60, 121.00, "alias: Metro Manila"),
    ("IT", "Lombardy"): (45.46, 9.19, "alias: Milan"),
    ("PE", "Lima"): (-12.05, -77.04, "alias: Lima city"),
    ("TJ", "Dushanbe"): (38.56, 68.78, "alias: Dushanbe"),
    ("SX", "Unknown"): (18.04, -63.05, "alias: Sint Maarten center"),
    ("SJ", "Svalbard"): (78.22, 15.65, "alias: Longyearbyen"),
}

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--points-output", type=Path, default=DEFAULT_POINTS)
    parser.add_argument("--metric", choices=METRICS, default="unique_streams")
    parser.add_argument("--admin1-geojson", type=Path, default=CACHE_DIR / "ne_10m_admin_1_states_provinces.geojson")
    parser.add_argument("--countries-geojson", type=Path, default=CACHE_DIR / "ne_110m_admin_0_countries.geojson")
    parser.add_argument("--title")
    return parser.parse_args()


def normalized(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def core(value: str | None) -> str:
    stop = {
        "province", "region", "district", "department", "state", "county",
        "city", "capital", "autonomous", "federal", "municipality", "oblast",
        "metropolitan", "administrative", "territory", "atoll", "islands", "island",
        "of", "the", "de", "da", "do", "del", "la", "le", "distrikt", "hot",
    }
    tokens = [token for token in normalized(value).split() if token not in stop]
    return " ".join(tokens) or normalized(value)


def download(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "starlink-measure-region-bubbles/1.0"})
    try:
        with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=60) as response:
            payload = response.read()
        json.loads(payload)
    except Exception as exc:  # pragma: no cover - network-dependent
        raise RuntimeError(f"could not download map data from {url}: {exc}") from exc
    path.write_bytes(payload)


def load_json(path: Path, url: str) -> Mapping[str, Any]:
    if not path.is_file():
        print(f"Downloading {path}", file=sys.stderr)
        download(path, url)
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("type") != "FeatureCollection":
        raise ValueError(f"{path} is not a GeoJSON FeatureCollection")
    return data


def load_rows(path: Path, metric: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"country", "country_code", "region", metric}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path} is missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            try:
                value = float(row[metric])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value) or value <= 0:
                continue
            rows.append({**row, "value": value, "country_code": row["country_code"].upper()})
    return rows


def properties_text(properties: Mapping[str, Any]) -> list[str]:
    return [str(properties.get(key) or "") for key in (
        "name", "name_en", "name_alt", "name_local", "gn_name", "gns_name",
        "woe_name", "region", "region_sub", "geonunit", "admin",
    )]


def match_region(row: Mapping[str, Any], admin1_by_country: Mapping[str, list[Mapping[str, Any]]], country_points: Mapping[str, tuple[float, float]]) -> tuple[float, float, str, str]:
    code, region = row["country_code"], row["region"]
    alias = ALIASES.get((code, region))
    if alias:
        lat, lon, method = alias
        return lat, lon, method, ""
    candidates = admin1_by_country.get(code, [])
    query, query_core = normalized(region), core(region)
    best_score, best = 0.0, None
    for feature in candidates:
        properties = feature.get("properties", {})
        for field in properties_text(properties):
            candidate, candidate_core = normalized(field), core(field)
            if not candidate:
                continue
            if query == candidate:
                score = 1.0
            elif query_core and query_core == candidate_core:
                score = 0.98
            elif len(query) >= 4 and (query in candidate or candidate in query):
                score = 0.91
            elif len(query_core) >= 4 and (query_core in candidate_core or candidate_core in query_core):
                score = 0.89
            else:
                score = max(difflib.SequenceMatcher(None, query, candidate).ratio(), difflib.SequenceMatcher(None, query_core, candidate_core).ratio()) * 0.85
            if score > best_score:
                best_score, best = score, feature
    # Only accept a clear name match.  Fuzzy fallback can be wildly wrong for
    # broad labels such as "England" or "Western Province".
    if best is not None and best_score >= 0.89:
        properties = best["properties"]
        lat, lon = float(properties["latitude"]), float(properties["longitude"])
        return lat, lon, f"admin1:{properties.get('name_en') or properties.get('name')}", f"match_score:{best_score:.2f}"
    if code in country_points:
        lat, lon = country_points[code]
        return lat, lon, "country representative point", f"no admin1 match (best_score:{best_score:.2f})"
    return float("nan"), float("nan"), "unmatched", f"no map feature (best_score:{best_score:.2f})"


def feature_code(feature: Mapping[str, Any]) -> str:
    properties = feature.get("properties", {})
    return str(properties.get("ISO_A2_EH") or properties.get("ISO_A2") or "").upper()


def metric_label(metric: str) -> str:
    return {"rows": "records", "unique_client_ips": "unique client IPs", "unique_streams": "unique streams"}[metric]


def geometry_rings(geometry: Mapping[str, Any]):
    coordinates = geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        yield from coordinates
    elif geometry.get("type") == "MultiPolygon":
        for polygon in coordinates:
            yield from polygon


def draw(
    rows: list[dict[str, Any]],
    countries: Mapping[str, Any],
    output: Path,
    metric: str,
    title: str | None,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.colors as colors
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("matplotlib and numpy are required; run `uv sync --no-install-project`") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    valid = [row for row in rows if math.isfinite(row["latitude"]) and math.isfinite(row["longitude"])]
    values = np.array([row["value"] for row in valid], dtype=float)
    vmax = max(float(values.max(initial=1)), 1.0)
    norm = colors.LogNorm(vmin=1, vmax=vmax)
    cmap = plt.get_cmap("turbo")
    sizes = 28 + 470 * np.sqrt(values / vmax)
    figure, axis = plt.subplots(figsize=(15, 8.2), facecolor="white")
    axis.set_facecolor("#eef2f5")
    for feature in countries["features"]:
        for ring in geometry_rings(feature.get("geometry") or {}):
            if len(ring) < 2:
                continue
            xs, ys = zip(*ring)
            axis.fill(xs, ys, color="#f9fafb", zorder=0)
            axis.plot(xs, ys, color="#a7b1bc", linewidth=0.35, zorder=1)
    axis.scatter(
        [row["longitude"] for row in valid],
        [row["latitude"] for row in valid],
        s=sizes,
        c=values,
        cmap=cmap,
        norm=norm,
        alpha=0.78,
        edgecolors="white",
        linewidths=0.8,
        zorder=3,
    )
    axis.set_xlim(-180, 180)
    axis.set_ylim(-60, 90)
    axis.set_aspect("equal", adjustable="box")
    axis.axis("off")
    figure.suptitle(
        title or f"Region-level push-stream demand ({metric_label(metric)})",
        x=0.08,
        y=0.965,
        ha="left",
        fontsize=18,
        fontweight="bold",
    )
    figure.text(
        0.08,
        0.925,
        "Bubble area and color encode demand; locations are administrative-region representative points or documented fallbacks.",
        fontsize=9.5,
        color="#52606d",
    )
    colorbar = figure.colorbar(
        plt.cm.ScalarMappable(norm=norm, cmap=cmap),
        ax=axis,
        orientation="horizontal",
        fraction=0.035,
        pad=0.025,
        aspect=45,
    )
    colorbar.set_label(f"{metric_label(metric).capitalize()} (log scale)")
    colorbar.outline.set_visible(False)
    top = sorted(valid, key=lambda item: item["value"], reverse=True)[:10]
    top_items = [f"{row['region']} {row['value']:g}" for row in top]
    top_text = "Top 10: " + "  |  ".join(top_items[:5])
    if len(top_items) > 5:
        top_text += "\n         " + "  |  ".join(top_items[5:])
    figure.text(0.08, 0.045, top_text, ha="left", va="bottom", fontsize=7.5, color="#52606d")
    figure.text(
        0.92,
        0.018,
        "Boundaries and representative points: Natural Earth",
        ha="right",
        fontsize=8,
        color="#52606d",
    )
    figure.tight_layout(rect=(0, 0.075, 1, 0.9))
    figure.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(figure)


def main() -> None:
    args = parse_args()
    rows = load_rows(args.input, args.metric)
    admin1 = load_json(args.admin1_geojson, ADMIN1_URL)
    countries = load_json(args.countries_geojson, COUNTRIES_URL)
    admin1_by_country: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for feature in admin1["features"]:
        admin1_by_country[str(feature.get("properties", {}).get("iso_a2") or "").upper()].append(feature)
    country_points: dict[str, tuple[float, float]] = {}
    for feature in countries["features"]:
        code = feature_code(feature)
        properties = feature.get("properties", {})
        if code and properties.get("LABEL_X") is not None and properties.get("LABEL_Y") is not None:
            country_points[code] = (float(properties["LABEL_Y"]), float(properties["LABEL_X"]))
    for row in rows:
        row["latitude"], row["longitude"], row["location_method"], row["location_note"] = match_region(row, admin1_by_country, country_points)
    args.points_output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["country", "country_code", "region", "value", "latitude", "longitude", "location_method", "location_note"]
    with args.points_output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)
    draw(rows, countries, args.output, args.metric, args.title)
    fallback_count = sum(not row["location_method"].startswith("admin1:") for row in rows)
    print(f"Wrote {args.output}")
    print(f"Wrote {args.points_output} ({len(rows)} points; {fallback_count} documented fallbacks)")


if __name__ == "__main__":
    main()
