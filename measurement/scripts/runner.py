#!/usr/bin/env python3
"""Unified trace/measurement runner.

The lifecycle implementation remains in ``starlive_measure.trace.trace_runner``;
this entrypoint only makes the measurement tree the natural command location.
"""

from __future__ import annotations

from starlive_measure.trace.trace_runner import main


if __name__ == "__main__":
    raise SystemExit(main())
