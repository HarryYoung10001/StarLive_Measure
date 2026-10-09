#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./extract_relevant_logcat_common.sh [LOGCAT_PATH_OR_RUN_DIR] [OUTPUT_PATH]
#
# Examples:
#   ./extract_relevant_logcat_common.sh
#   ./extract_relevant_logcat_common.sh ../logs/latest_logcat
#   ./extract_relevant_logcat_common.sh ../logs/latest_logcat/logcat.txt filtered.txt
#
# Purpose:
#   Extract logcat lines using a caller-provided pattern.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${BASE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LOG_ROOT="$BASE_DIR/logs"

INPUT="${1:-$LOG_ROOT/${LATEST_LINK:-latest_logcat}}"
OUTPUT="${2:-}"
PATTERN="${PATTERN:-${DEFAULT_PATTERN:-douyin|tiktok|aweme|media|audio}}"
EXTRACT_TYPE="${EXTRACT_TYPE:-logcat_extract}"
EXTRACT_NOTE="${EXTRACT_NOTE:-case-insensitive grep with line numbers over captured logcat text}"

resolve_input_log() {
  local input_path="$1"

  if [ -d "$input_path" ]; then
    printf '%s\n' "$input_path/logcat.txt"
  else
    printf '%s\n' "$input_path"
  fi
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

LOGCAT_PATH="$(resolve_input_log "$INPUT")"

if [ ! -f "$LOGCAT_PATH" ]; then
  fail "logcat file not found: $LOGCAT_PATH"
fi

RUN_DIR="$(cd "$(dirname "$LOGCAT_PATH")" && pwd)"
LOGCAT_BASENAME="$(basename "$LOGCAT_PATH")"

if [ -z "$OUTPUT" ]; then
  if [ "$LOGCAT_BASENAME" = "logcat.txt" ]; then
    OUTPUT="$RUN_DIR/logcat_relevant.txt"
  else
    OUTPUT="$RUN_DIR/${LOGCAT_BASENAME%.*}_relevant.txt"
  fi
fi

OUTPUT_DIR="$(dirname "$OUTPUT")"
mkdir -p "$OUTPUT_DIR"

set +e
grep -Ein "$PATTERN" "$LOGCAT_PATH" > "$OUTPUT"
GREP_RC=$?
set -e

if [ "$GREP_RC" -ne 0 ] && [ "$GREP_RC" -ne 1 ]; then
  fail "grep failed with exit code $GREP_RC"
fi

MATCH_COUNT="$(wc -l < "$OUTPUT" | tr -d ' ')"
SUMMARY_PATH="$RUN_DIR/logcat_relevant_summary.txt"
METADATA_PATH="$RUN_DIR/logcat_relevant_metadata.txt"

{
  printf 'pattern=%s\n' "$PATTERN"
  printf 'input=%s\n' "$LOGCAT_PATH"
  printf 'output=%s\n' "$OUTPUT"
  printf 'match_count=%s\n' "$MATCH_COUNT"
} > "$SUMMARY_PATH"

{
  printf 'type=%s\n' "$EXTRACT_TYPE"
  printf 'pattern=%s\n' "$PATTERN"
  printf 'input=%s\n' "$LOGCAT_PATH"
  printf 'output=%s\n' "$OUTPUT"
  printf 'match_count=%s\n' "$MATCH_COUNT"
  printf 'extract_time_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'hostname=%s\n' "$(hostname)"
  printf 'note=%s\n' "$EXTRACT_NOTE"
} > "$METADATA_PATH"

echo "Done."
echo "Input:       $LOGCAT_PATH"
echo "Output:      $OUTPUT"
echo "Pattern:     $PATTERN"
echo "Matches:     $MATCH_COUNT"
echo "Summary:     $SUMMARY_PATH"
