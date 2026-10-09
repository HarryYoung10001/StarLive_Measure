#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BASE_DIR="${BASE_DIR:-$SCRIPT_DIR/..}"
BASE_DIR="$BASE_DIR" \
RUN_PREFIX=douyin_logcat \
RUN_TYPE=douyin_logcat \
LATEST_LINK=latest_douyin_logcat \
DEFAULT_RUN_LABEL=douyin \
RUN_NOTE='captures Douyin/Android logcat buffers' \
  bash "$REPO_ROOT/runtime/android/scripts/capture_adb_logs_common.sh" "$@"
