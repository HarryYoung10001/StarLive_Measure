#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: capture_dual_adb_logs.sh --push-device SERIAL --pull-device SERIAL --duration SECONDS [--label LABEL]

Capture logcat from a push-stream device, start the pull-stream capture 5 seconds
later, stop the pull-stream capture after SECONDS, then stop the push-stream
capture after another 5 seconds.
USAGE
}

PUSH_DEVICE=""
PULL_DEVICE=""
DURATION=""
RUN_LABEL="demo_test"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --push-device)
      [[ $# -ge 2 ]] || { echo "ERROR: --push-device requires a value" >&2; usage >&2; exit 2; }
      PUSH_DEVICE="$2"
      shift 2
      ;;
    --pull-device)
      [[ $# -ge 2 ]] || { echo "ERROR: --pull-device requires a value" >&2; usage >&2; exit 2; }
      PULL_DEVICE="$2"
      shift 2
      ;;
    --duration)
      [[ $# -ge 2 ]] || { echo "ERROR: --duration requires a value" >&2; usage >&2; exit 2; }
      DURATION="$2"
      shift 2
      ;;
    --label)
      [[ $# -ge 2 ]] || { echo "ERROR: --label requires a value" >&2; usage >&2; exit 2; }
      RUN_LABEL="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -n "$PUSH_DEVICE" ]] || { echo "ERROR: --push-device is required" >&2; usage >&2; exit 2; }
[[ -n "$PULL_DEVICE" ]] || { echo "ERROR: --pull-device is required" >&2; usage >&2; exit 2; }
[[ -n "$DURATION" ]] || { echo "ERROR: --duration is required" >&2; usage >&2; exit 2; }
[[ "$PUSH_DEVICE" != "$PULL_DEVICE" ]] || { echo "ERROR: push and pull devices must be different" >&2; exit 2; }
if ! awk -v d="$DURATION" 'BEGIN { exit !(d > 0) }'; then
  echo "ERROR: --duration must be a positive number: $DURATION" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${BASE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LOG_ROOT="$BASE_DIR/logs"
CLEAR_LOGCAT="${CLEAR_LOGCAT:-0}"
LOGCAT_ARGS="${LOGCAT_ARGS:--b all -v threadtime}"
PUSH_EXPECTED_DURATION="$(awk -v d="$DURATION" 'BEGIN { print d + 10 }')"
SAFE_LABEL="$(printf '%s' "$RUN_LABEL" | tr -c 'A-Za-z0-9._-' '_')"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="demo_dual_logcat_${TS}_${DURATION}s_${SAFE_LABEL}"
RUN_DIR="$LOG_ROOT/$RUN_ID"
PUSH_DIR="$RUN_DIR/push"
PULL_DIR="$RUN_DIR/pull"
mkdir -p "$PUSH_DIR" "$PULL_DIR"

fail() {
  echo "ERROR: $*" | tee "$RUN_DIR/error.txt" >&2
  exit 1
}

command -v adb >/dev/null 2>&1 || fail "adb not found on Mac. Run this from the Mac/Android control machine."
adb devices > "$RUN_DIR/adb_devices.txt" 2>&1 || fail "failed to list adb devices"

device_is_ready() {
  local serial="$1"
  awk -v serial="$serial" '$1 == serial && $2 == "device" { found=1 } END { exit !found }' "$RUN_DIR/adb_devices.txt"
}

device_is_ready "$PUSH_DEVICE" || fail "push device is not connected in adb device state: $PUSH_DEVICE"
device_is_ready "$PULL_DEVICE" || fail "pull device is not connected in adb device state: $PULL_DEVICE"

collect_phone_info() {
  local device="$1"
  local output_dir="$2"
  adb -s "$device" shell "uname -a; getprop ro.product.model; getprop ro.product.cpu.abi; getprop ro.build.version.sdk; date; ip route; cmd wifi status 2>/dev/null || true" \
    > "$output_dir/phone_info.txt" 2>&1 || true
}

collect_phone_info "$PUSH_DEVICE" "$PUSH_DIR"
collect_phone_info "$PULL_DEVICE" "$PULL_DIR"

if [[ "$CLEAR_LOGCAT" == "1" ]]; then
  adb -s "$PUSH_DEVICE" logcat -c > "$PUSH_DIR/logcat_clear.stdout" 2> "$PUSH_DIR/logcat_clear.stderr" \
    || fail "failed to clear push device logcat buffers"
  adb -s "$PULL_DEVICE" logcat -c > "$PULL_DIR/logcat_clear.stdout" 2> "$PULL_DIR/logcat_clear.stderr" \
    || fail "failed to clear pull device logcat buffers"
fi

cat > "$RUN_DIR/metadata.txt" <<META
run_id=$RUN_ID
type=demo_dual_logcat
pull_duration_s=$DURATION
push_expected_duration_s=$PUSH_EXPECTED_DURATION
stagger_s=5
run_label=$RUN_LABEL
safe_label=$SAFE_LABEL
push_device=$PUSH_DEVICE
pull_device=$PULL_DEVICE
clear_logcat=$CLEAR_LOGCAT
logcat_args=$LOGCAT_ARGS
push_logcat_path=$PUSH_DIR/logcat.txt
pull_logcat_path=$PULL_DIR/logcat.txt
mac_time_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
mac_hostname=$(hostname)
note=pull duration excludes the 5-second push lead-in and 5-second push tail
META

PUSH_PID=""
PULL_PID=""
PUSH_RC=""
PULL_RC=""
FINALIZED=0

stop_capture() {
  local pid="$1"
  local role="$2"
  local rc=0
  [[ -n "$pid" ]] || return 0
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid" >/dev/null 2>&1 || true
  fi
  set +e
  wait "$pid" 2>/dev/null
  rc=$?
  set -e
  if [[ "$rc" -eq 143 ]]; then
    rc=0
  fi
  if [[ "$role" == "push" ]]; then
    PUSH_RC="$rc"
  else
    PULL_RC="$rc"
  fi
}

finalize() {
  local exit_rc="$1"
  [[ "$FINALIZED" -eq 0 ]] || return
  FINALIZED=1
  stop_capture "$PULL_PID" pull
  stop_capture "$PUSH_PID" push
  [[ -n "$PUSH_RC" ]] || PUSH_RC="$exit_rc"
  [[ -n "$PULL_RC" ]] || PULL_RC="$exit_rc"
  printf '%s\n' "$PUSH_RC" > "$RUN_DIR/push_exit_code.txt"
  printf '%s\n' "$PULL_RC" > "$RUN_DIR/pull_exit_code.txt"
  cat >> "$RUN_DIR/metadata.txt" <<META
push_exit_code=$PUSH_RC
pull_exit_code=$PULL_RC
META
  ln -sfn "$(basename "$RUN_DIR")" "$LOG_ROOT/latest_demo_dual_logcat"
}

on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  finalize "$rc"
  exit "$rc"
}

on_signal() {
  local rc="$1"
  exit "$rc"
}

trap on_exit EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

start_capture() {
  local device="$1"
  local output_dir="$2"
  local since
  local -a args
  date -u +%Y-%m-%dT%H:%M:%SZ > "$output_dir/mac_start_time_utc.txt"
  adb -s "$device" shell "date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date" \
    > "$output_dir/android_start_time.txt" 2>&1 || true
  since="$(adb -s "$device" shell "date '+%m-%d %H:%M:%S.000' 2>/dev/null" | tr -d '\r' || true)"
  printf '%s\n' "$since" > "$output_dir/logcat_since.txt"
  read -r -a args <<< "$LOGCAT_ARGS"
  if [[ -n "$since" ]]; then
    adb -s "$device" logcat "${args[@]}" -T "$since" > "$output_dir/logcat.txt" 2> "$output_dir/logcat.stderr" &
  else
    adb -s "$device" logcat "${args[@]}" > "$output_dir/logcat.txt" 2> "$output_dir/logcat.stderr" &
  fi
  CAPTURE_PID=$!
}

finish_role() {
  local pid="$1"
  local role="$2"
  local device="$3"
  local output_dir="$4"
  stop_capture "$pid" "$role"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$output_dir/mac_end_time_utc.txt"
  adb -s "$device" shell "date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date" \
    > "$output_dir/android_end_time.txt" 2>&1 || true
}

echo "Starting push-device logcat from $PUSH_DEVICE"
echo "Logs: $RUN_DIR"
start_capture "$PUSH_DEVICE" "$PUSH_DIR"
PUSH_PID="$CAPTURE_PID"
sleep 5

echo "Starting pull-device logcat from $PULL_DEVICE for $DURATION seconds"
start_capture "$PULL_DEVICE" "$PULL_DIR"
PULL_PID="$CAPTURE_PID"
sleep "$DURATION"

echo "Stopping pull-device logcat"
finish_role "$PULL_PID" pull "$PULL_DEVICE" "$PULL_DIR"
PULL_PID=""
sleep 5

echo "Stopping push-device logcat"
finish_role "$PUSH_PID" push "$PUSH_DEVICE" "$PUSH_DIR"
PUSH_PID=""

FINAL_RC=0
if [[ "$PUSH_RC" -ne 0 ]]; then
  FINAL_RC="$PUSH_RC"
elif [[ "$PULL_RC" -ne 0 ]]; then
  FINAL_RC="$PULL_RC"
fi
finalize "$FINAL_RC"
trap - EXIT INT TERM

if [[ "$FINAL_RC" -ne 0 ]]; then
  echo "WARNING: logcat capture failed (push=$PUSH_RC, pull=$PULL_RC)" | tee "$RUN_DIR/error.txt" >&2
  exit "$FINAL_RC"
fi
echo "Done. Logs saved to: $RUN_DIR"
