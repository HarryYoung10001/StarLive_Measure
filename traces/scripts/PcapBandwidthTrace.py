"""Compatibility CLI for StarLive PCAP bandwidth processing."""

from starlive_measure.trace.PcapBandwidthTrace import *
from starlive_measure.trace.PcapBandwidthTrace import main


if __name__ == "__main__":
    raise SystemExit(main())
