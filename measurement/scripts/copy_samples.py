#!/usr/bin/env python3
"""Copy a compact, reproducible sample set from ``traces/logs``."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "traces" / "logs"
TARGET_ROOT = ROOT / "measurement" / "logs"
REFERENCE_SOURCE = ROOT / "traces" / "reference"
REFERENCE_TARGET = ROOT / "measurement" / "reference"

SAMPLE_SESSIONS = (
    "udp_downlink_custom_300pps_1200B_150s_20260821T143401Z",
    "udp_uplink_custom_300pps_1200B_150s_20260820T071706Z",
)
HIGH_RATE_SESSIONS = (
    "udp_uplink_custom_80M_150s_20260819T191622Z",
    "udp_downlink_custom_150M_1200B_150s_20260821T094224Z",
)


def _complete(run: Path) -> bool:
    try:
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return manifest.get("status") == "complete" and manifest.get("custom_udp", {}).get("status") == "complete"


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_run(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "profile.json", "events.jsonl"):
        path = source / name
        if path.is_file():
            _copy_file(path, target / name)
    for relative in (
        "derived",
        "roles/server",
        "roles/udp_sender",
        "roles/udp_receiver",
    ):
        source_dir = source / relative
        if not source_dir.is_dir():
            continue
        for path in source_dir.rglob("*"):
            if not path.is_file() or "remote" in path.parts:
                continue
            if path.suffix.lower() in {".png", ".pcap", ".pcapng", ".gz"}:
                continue
            _copy_file(path, target / path.relative_to(source))


def copy_session(name: str, *, limit: int | None = None) -> int:
    source = SOURCE_ROOT / name
    if not source.is_dir():
        print(f"WARNING: missing source session {source}")
        return 0
    runs = [path for path in sorted(source.glob("*/manifest.json")) if _complete(path.parent)]
    if limit is not None:
        runs = runs[:limit]
    for manifest in runs:
        run = manifest.parent
        copy_run(run, TARGET_ROOT / name / run.name)
    session_json = source / "session.json"
    if session_json.is_file():
        _copy_file(session_json, TARGET_ROOT / name / "session.json")
    return len(runs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="remove only measurement/logs before copying")
    args = parser.parse_args(argv)
    if args.clean and TARGET_ROOT.is_dir():
        shutil.rmtree(TARGET_ROOT)
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    REFERENCE_TARGET.mkdir(parents=True, exist_ok=True)
    for reference in REFERENCE_SOURCE.glob("*.pdf"):
        _copy_file(reference, REFERENCE_TARGET / reference.name)
    copied = 0
    for name in SAMPLE_SESSIONS:
        copied += copy_session(name)
    for name in HIGH_RATE_SESSIONS:
        copied += copy_session(name, limit=1)
    print(f"Copied {copied} complete sample runs to {TARGET_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
