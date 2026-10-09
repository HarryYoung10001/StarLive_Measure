#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./capture_adb_logs_common.sh [DURATION_S] [RUN_LABEL]
#
# Examples:
#   ./capture_adb_logs_common.sh 60 douyin
#   CLEAR_LOGCAT=1 ./capture_adb_logs_common.sh 30 login_smoke
#
# Purpose:
#   Capture Android/Reemo cloud phone logcat logs through adb for a bounded
#   time window and save local run artifacts under the caller's logs directory.

DURATION="${1:-60}"
RUN_LABEL="${2:-${DEFAULT_RUN_LABEL:-capture}}"
RUN_PREFIX="${RUN_PREFIX:-logcat}"
RUN_TYPE="${RUN_TYPE:-logcat}"
LATEST_LINK="${LATEST_LINK:-latest_logcat}"
RUN_NOTE="${RUN_NOTE:-captures adb-visible Android logcat buffers}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${BASE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LOG_ROOT="$BASE_DIR/logs"
CLEAR_LOGCAT="${CLEAR_LOGCAT:-0}"
LOGCAT_ARGS="${LOGCAT_ARGS:--b all -v threadtime}"
LOGCAT_SINCE="${LOGCAT_SINCE:-}"

if ! awk -v d="$DURATION" 'BEGIN { exit !(d > 0) }'; then
  echo "ERROR: DURATION_S must be a positive number: $DURATION" >&2
  exit 2
fi

SAFE_LABEL="$(printf '%s' "$RUN_LABEL" | tr -c 'A-Za-z0-9._-' '_')"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="${RUN_PREFIX}_${TS}_${DURATION}s_${SAFE_LABEL}"
RUN_DIR="$LOG_ROOT/$RUN_ID"

mkdir -p "$RUN_DIR"

fail() {
  echo "ERROR: $*" | tee "$RUN_DIR/error.txt" >&2
  exit 1
}

command -v adb >/dev/null 2>&1 || fail "adb not found on Mac. Run this from the Mac/Reemo control machine."

DEVICE="${ADB_DEVICE:-$(adb devices | awk '$2=="device"{print $1; exit}')}"
adb devices > "$RUN_DIR/adb_devices.txt" 2>&1 || true

if [ -z "$DEVICE" ]; then
  fail "no adb device found. Run: hb adb connect -d <DEVICE_ID>"
fi

adb -s "$DEVICE" shell "uname -a; getprop ro.product.model; getprop ro.product.cpu.abi; getprop ro.build.version.sdk; date; ip route; cmd wifi status 2>/dev/null || true" \
  > "$RUN_DIR/phone_info.txt" 2>&1 || true

if [ "$CLEAR_LOGCAT" = "1" ]; then
  adb -s "$DEVICE" logcat -c > "$RUN_DIR/logcat_clear.stdout" 2> "$RUN_DIR/logcat_clear.stderr" || fail "failed to clear logcat buffers"
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "$RUN_DIR/mac_start_time_utc.txt"
adb -s "$DEVICE" shell "date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date" > "$RUN_DIR/android_start_time.txt" 2>&1 || true
if [ -z "$LOGCAT_SINCE" ]; then
  LOGCAT_SINCE="$(adb -s "$DEVICE" shell "date '+%m-%d %H:%M:%S.000' 2>/dev/null" | tr -d '\r' || true)"
fi

cat > "$RUN_DIR/metadata.txt" <<META
run_id=$RUN_ID
type=$RUN_TYPE
duration_s=$DURATION
run_label=$RUN_LABEL
safe_label=$SAFE_LABEL
adb_device=$DEVICE
clear_logcat=$CLEAR_LOGCAT
logcat_args=$LOGCAT_ARGS
logcat_since=$LOGCAT_SINCE
logcat_path=$RUN_DIR/logcat.txt
mac_time_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
mac_hostname=$(hostname)
note=$RUN_NOTE; root-only logs and app-private files are out of scope
META

echo "============================================================"
echo "Capturing Android/Reemo logcat"
echo "Device:    $DEVICE"
echo "Duration:  $DURATION s"
echo "Label:     $RUN_LABEL"
echo "Args:      $LOGCAT_ARGS"
echo "Since:     $LOGCAT_SINCE"
echo "Clear:     $CLEAR_LOGCAT"
echo "Log dir:   $RUN_DIR"
echo "============================================================"

set +e
if command -v timeout >/dev/null 2>&1; then
  if [ -n "$LOGCAT_SINCE" ]; then
    timeout "$DURATION" adb -s "$DEVICE" logcat $LOGCAT_ARGS -T "$LOGCAT_SINCE" \
      > "$RUN_DIR/logcat.txt" \
      2> "$RUN_DIR/logcat.stderr"
  else
    timeout "$DURATION" adb -s "$DEVICE" logcat $LOGCAT_ARGS \
      > "$RUN_DIR/logcat.txt" \
      2> "$RUN_DIR/logcat.stderr"
  fi
  RC=$?
elif command -v gtimeout >/dev/null 2>&1; then
  if [ -n "$LOGCAT_SINCE" ]; then
    gtimeout "$DURATION" adb -s "$DEVICE" logcat $LOGCAT_ARGS -T "$LOGCAT_SINCE" \
      > "$RUN_DIR/logcat.txt" \
      2> "$RUN_DIR/logcat.stderr"
  else
    gtimeout "$DURATION" adb -s "$DEVICE" logcat $LOGCAT_ARGS \
      > "$RUN_DIR/logcat.txt" \
      2> "$RUN_DIR/logcat.stderr"
  fi
  RC=$?
else
  if [ -n "$LOGCAT_SINCE" ]; then
    adb -s "$DEVICE" logcat $LOGCAT_ARGS -T "$LOGCAT_SINCE" \
      > "$RUN_DIR/logcat.txt" \
      2> "$RUN_DIR/logcat.stderr" &
  else
    adb -s "$DEVICE" logcat $LOGCAT_ARGS \
      > "$RUN_DIR/logcat.txt" \
      2> "$RUN_DIR/logcat.stderr" &
  fi
  LOGCAT_PID="$!"
  sleep "$DURATION"
  kill "$LOGCAT_PID" >/dev/null 2>&1 || true
  wait "$LOGCAT_PID" 2>/dev/null
  RC=$?
fi
set -e

date -u +%Y-%m-%dT%H:%M:%SZ > "$RUN_DIR/mac_end_time_utc.txt"
adb -s "$DEVICE" shell "date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date" > "$RUN_DIR/android_end_time.txt" 2>&1 || true

if [ "$RC" -eq 124 ] || [ "$RC" -eq 143 ]; then
  RC=0
fi

echo "$RC" > "$RUN_DIR/exit_code.txt"
ln -sfn "$(basename "$RUN_DIR")" "$LOG_ROOT/$LATEST_LINK"

if [ "$RC" -ne 0 ]; then
  echo "WARNING: logcat capture failed, exit code=$RC"
  echo "See: $RUN_DIR/logcat.stderr"
  exit "$RC"
fi

echo "Done."
echo "Logs saved to: $RUN_DIR"
