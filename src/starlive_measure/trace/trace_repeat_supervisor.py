#!/usr/bin/env python3
"""Supervise a detached trace_runner repeat process and record its progress."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from .trace_runtime import validate_custom_run_success
from starlive_measure.paths import REPO_ROOT


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def successful_runs(
    session: Path,
    min_span_s: float,
    max_span_s: float,
    environment_generation: int | None = None,
) -> list[Path]:
    runs: list[Path] = []
    for manifest in sorted(session.glob("*/manifest.json")):
        if environment_generation is not None:
            try:
                metadata = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if int(metadata.get("environment", {}).get("generation", 1) or 1) != environment_generation:
                continue
        if validate_custom_run_success(
            manifest.parent, min_span_s=min_span_s, max_span_s=max_span_s
        ):
            runs.append(manifest.parent)
    return runs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--min-span-s", type=float, default=145)
    parser.add_argument("--max-span-s", type=float, default=160)
    parser.add_argument("--first-successes", type=int, default=2)
    parser.add_argument("--environment-generation", type=int)
    parser.add_argument("--poll-s", type=float, default=5)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a runner command is required after --")

    session = args.session_dir.resolve()
    session.mkdir(parents=True, exist_ok=True)
    stdout_path = session / "continuation_runner.stdout"
    stderr_path = session / "continuation_runner.stderr"
    status_path = session / "continuation_supervisor_status.json"
    suffix = f"_generation_{args.environment_generation}" if args.environment_generation is not None else ""
    first_path = session / f"first_two_successes{suffix}.json"

    with stdout_path.open("ab", buffering=0) as stdout, stderr_path.open("ab", buffering=0) as stderr:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        (session / "continuation_runner.pid").write_text(f"{process.pid}\n", encoding="ascii")
        write_json(status_path, {
            "status": "running",
            "supervisor_pid": os.getpid(),
            "runner_pid": process.pid,
            "command": command,
            "successful_runs": 0,
        })
        first_recorded = first_path.is_file()
        while process.poll() is None:
            runs = successful_runs(session, args.min_span_s, args.max_span_s, args.environment_generation)
            if len(runs) >= args.first_successes and not first_recorded:
                write_json(first_path, {
                    "status": "complete",
                    "successful_runs": len(runs),
                    "validated_runs": [run.name for run in runs[: args.first_successes]],
                    "min_span_s": args.min_span_s,
                    "max_span_s": args.max_span_s,
                    "environment_generation": args.environment_generation,
                })
                first_recorded = True
            write_json(status_path, {
                "status": "running",
                "supervisor_pid": os.getpid(),
                "runner_pid": process.pid,
                "command": command,
                "successful_runs": len(runs),
                "environment_generation": args.environment_generation,
                "first_successes_validated": first_recorded,
            })
            time.sleep(max(0.5, args.poll_s))

    runs = successful_runs(session, args.min_span_s, args.max_span_s, args.environment_generation)
    write_json(status_path, {
        "status": "complete" if process.returncode == 0 else "failed",
        "supervisor_pid": os.getpid(),
        "runner_pid": process.pid,
        "runner_exit_code": process.returncode,
        "command": command,
        "successful_runs": len(runs),
        "environment_generation": args.environment_generation,
        "first_successes_validated": first_path.is_file(),
    })
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
