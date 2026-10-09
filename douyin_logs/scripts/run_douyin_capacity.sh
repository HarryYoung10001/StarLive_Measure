#!/usr/bin/env bash
set -euo pipefail

IPERF_DEVICE="${1:-}"; SERVER="${2:-}"; RATE="${3:-180M}"; DURATION="${4:-150}"; INTERVAL="${5:-0.1}"; PORT="${6:-5201}"; LEN="${7:-1200}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; BASE_DIR="${BASE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"; LOG_ROOT="$BASE_DIR/logs"; IPERF_BIN="${IPERF_BIN:-/data/local/tmp/iperf3}"
fail() { echo "ERROR: $*" >&2; exit 1; }
[[ -n "$IPERF_DEVICE" && -n "$SERVER" ]] || fail "usage: $0 <IPERF_DEVICE> <SERVER> [RATE] [DURATION] [INTERVAL] [PORT] [LEN]"
command -v adb >/dev/null 2>&1 || fail "adb not found"
DEVICES=()
while IFS= read -r device; do
  [[ -n "$device" ]] && DEVICES+=("$device")
done < <(adb devices | awk '$2 == "device" {print $1}')
[[ ${#DEVICES[@]} -eq 2 ]] || fail "expected exactly two ready adb devices, found ${#DEVICES[@]}"
awk -v value="$DURATION" 'BEGIN {exit !(value > 0)}' || fail "DURATION must be positive: $DURATION"
awk -v value="$INTERVAL" 'BEGIN {exit !(value > 0)}' || fail "INTERVAL must be positive: $INTERVAL"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || fail "PORT must be between 1 and 65535: $PORT"
[[ "$LEN" =~ ^[0-9]+$ ]] && (( LEN > 0 )) || fail "LEN must be positive: $LEN"
printf '%s\n' "${DEVICES[@]}" | grep -Fxq "$IPERF_DEVICE" || fail "iperf device is not one of the two ready devices: $IPERF_DEVICE"
for d in "${DEVICES[@]}"; do m="$(adb -s "$d" shell getprop ro.product.manufacturer 2>/dev/null | tr -d '\r')"; model="$(adb -s "$d" shell getprop ro.product.model 2>/dev/null | tr -d '\r')"; echo "[device] serial=$d manufacturer=${m:-unknown} model=${model:-unknown}"; done
for d in "${DEVICES[@]}"; do [[ "$d" != "$IPERF_DEVICE" ]] && LOG_DEVICE="$d"; done
adb -s "$IPERF_DEVICE" shell "test -x '$IPERF_BIN'" >/dev/null 2>&1 || fail "$IPERF_BIN is not executable on $IPERF_DEVICE"
TS="$(date -u +%Y%m%dT%H%M%SZ)"; RUN_ID="douyin_capacity_${TS}_${RATE}_${DURATION}s"; RUN_DIR="$LOG_ROOT/$RUN_ID"; mkdir -p "$RUN_DIR"
adb devices -l > "$RUN_DIR/adb_devices.txt" 2>&1 || true
for d in "${DEVICES[@]}"; do adb -s "$d" shell "uname -a; getprop ro.product.manufacturer; getprop ro.product.model; getprop ro.product.cpu.abi; date; ip route" > "$RUN_DIR/phone_${d//[:\/]/_}.txt" 2>&1 || true; done
REMOTE_CMD="$IPERF_BIN -c $SERVER -p $PORT -u -R -b $RATE -l $LEN -t $DURATION -i $INTERVAL -J --get-server-output"; printf '%s\n' "$REMOTE_CMD" > "$RUN_DIR/remote_cmd.txt"
cat > "$RUN_DIR/metadata.txt" <<META
type=douyin_capacity
iperf_device=$IPERF_DEVICE
logcat_device=$LOG_DEVICE
server=$SERVER
port=$PORT
rate=$RATE
duration_s=$DURATION
interval_s=$INTERVAL
udp_payload_len=$LEN
iperf_bin=$IPERF_BIN
META
set +e
adb -s "$IPERF_DEVICE" shell "$REMOTE_CMD" > "$RUN_DIR/iperf3_downlink_client.json" 2> "$RUN_DIR/iperf3_downlink_client.stderr" & IPERF_PID=$!
BASE_DIR="$RUN_DIR/douyin_capture" ADB_DEVICE="$LOG_DEVICE" bash "$SCRIPT_DIR/capture_adb_logs.sh" "$DURATION" "$RUN_ID" > "$RUN_DIR/logcat_wrapper.stdout" 2> "$RUN_DIR/logcat_wrapper.stderr" & LOG_PID=$!
trap 'kill "$IPERF_PID" "$LOG_PID" 2>/dev/null || true' INT TERM
IPERF_DONE=0; LOG_DONE=0; IPERF_RC=0; LOG_RC=0
while [[ "$IPERF_DONE" -eq 0 || "$LOG_DONE" -eq 0 ]]; do
  if [[ "$IPERF_DONE" -eq 0 ]] && ! kill -0 "$IPERF_PID" 2>/dev/null; then
    wait "$IPERF_PID"; IPERF_RC=$?; IPERF_DONE=1
    [[ "$IPERF_RC" -eq 0 ]] || kill "$LOG_PID" 2>/dev/null || true
  fi
  if [[ "$LOG_DONE" -eq 0 ]] && ! kill -0 "$LOG_PID" 2>/dev/null; then
    wait "$LOG_PID"; LOG_RC=$?; LOG_DONE=1
    [[ "$LOG_RC" -eq 0 ]] || kill "$IPERF_PID" 2>/dev/null || true
  fi
  [[ "$IPERF_DONE" -eq 1 && "$LOG_DONE" -eq 1 ]] || sleep 0.2
done
set -e
echo "$IPERF_RC" > "$RUN_DIR/iperf_exit_code.txt"; echo "$LOG_RC" > "$RUN_DIR/logcat_exit_code.txt"
CAPTURE_DIR="$(find "$RUN_DIR/douyin_capture/logs" -maxdepth 1 -type d -name 'douyin_logcat_*' -print -quit 2>/dev/null || true)"; [[ -n "$CAPTURE_DIR" ]] && ln -sfn "$(basename "$RUN_DIR")/douyin_capture/logs/$(basename "$CAPTURE_DIR")" "$LOG_ROOT/latest_douyin_logcat"
ln -sfn "$(basename "$RUN_DIR")" "$LOG_ROOT/latest_douyin_capacity"
[[ "$IPERF_RC" -eq 0 && "$LOG_RC" -eq 0 ]] || exit 1
echo "Done. Logs saved to: $RUN_DIR"
