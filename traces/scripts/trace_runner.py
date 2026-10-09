"""Compatibility CLI for the StarLive trace runner."""

from starlive_measure.trace.trace_runner import *
from starlive_measure.trace.trace_runner import main


if __name__ == "__main__":
    raise SystemExit(main())
