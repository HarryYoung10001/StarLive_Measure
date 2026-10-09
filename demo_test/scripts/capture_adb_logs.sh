#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BASE_DIR="${BASE_DIR:-$SCRIPT_DIR/..}"
BASE_DIR="$BASE_DIR" \
RUN_PREFIX=demo_logcat \
RUN_TYPE=demo_logcat \
LATEST_LINK=latest_demo_logcat \
DEFAULT_RUN_LABEL=demo_test \
RUN_NOTE='captures Android logcat buffers for demo tests' \
  bash "$REPO_ROOT/runtime/android/scripts/capture_adb_logs_common.sh" "$@"
