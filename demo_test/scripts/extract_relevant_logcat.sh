#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BASE_DIR="${BASE_DIR:-$SCRIPT_DIR/..}"
BASE_DIR="$BASE_DIR" \
LATEST_LINK=latest_demo_logcat \
DEFAULT_PATTERN=push_stream \
EXTRACT_TYPE=demo_logcat_extract \
EXTRACT_NOTE='case-insensitive grep over captured demo logcat text' \
  bash "$REPO_ROOT/runtime/android/scripts/extract_relevant_logcat_common.sh" "$@"
