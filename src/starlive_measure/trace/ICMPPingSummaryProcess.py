#!/usr/bin/env python3
"""Parse and validate an Android ping summary."""

from __future__ import annotations

import argparse
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


SUMMARY_RE = re.compile(
    r"(?P<transmitted>\d+)\s+packets\s+transmitted,\s*"
    r"(?P<received>\d+)(?:\s+packets)?\s+received,\s*"
    r"(?P<loss>[0-9]+(?:\.[0-9]+)?)%\s+packet\s+loss"
)


def parse_ping_summary(text: str) -> tuple[int, int, Decimal]:
    matches = list(SUMMARY_RE.finditer(text))
    if not matches:
        raise ValueError("ping packet summary not found")
    match = matches[-1]
    try:
        loss = Decimal(match.group("loss"))
    except InvalidOperation as error:
        raise ValueError("invalid packet loss percentage") from error
    return int(match.group("transmitted")), int(match.group("received")), loss


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Android ping.log path")
    parser.add_argument("--expected-transmitted", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.expected_transmitted <= 0:
        print("ERROR: --expected-transmitted must be positive", file=sys.stderr)
        return 2
    try:
        transmitted, received, loss = parse_ping_summary(
            args.input.read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, ValueError) as error:
        print(f"ERROR: failed to validate ping summary: {error}", file=sys.stderr)
        return 1
    if transmitted != args.expected_transmitted:
        print(
            f"ERROR: expected {args.expected_transmitted} transmitted packets, got {transmitted}",
            file=sys.stderr,
        )
        return 1
    if received <= 0:
        print("ERROR: ping received no replies", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(
            (
                f"icmp_transmitted_packets={transmitted}\n",
                f"icmp_received_packets={received}\n",
                f"icmp_packet_loss_percent={loss}\n",
            )
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
