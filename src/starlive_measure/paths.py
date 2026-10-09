"""Repository-relative paths shared by collection and analysis tools."""

from __future__ import annotations

import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
TRACE_ROOT = REPO_ROOT / "traces"
MEASUREMENT_ROOT = REPO_ROOT / "measurement"
ANDROID_ROOT = REPO_ROOT / "runtime" / "android"
SERVER_ROOT = REPO_ROOT / "runtime" / "server"
BUILD_ROOT = REPO_ROOT / ".trace-build"


def remote_server_repo(default: str = "/root/StarLive_Measure") -> str:
    """Return the configured remote repository path for server helpers."""

    return os.environ.get("STARLIVE_SERVER_REPO", default)
