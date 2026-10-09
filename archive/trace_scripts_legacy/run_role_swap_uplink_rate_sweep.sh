#!/usr/bin/env bash
set -euo pipefail

# Run a sequential uplink sweep with the heavy UDP and RTT phone roles
# swapped from the previous downlink experiment.

usage() {
  cat <<'USAGE'
Usage:
  SERVER_SSH=sgp-server bash traces/scripts/_archived/run_role_swap_uplink_rate_sweep.sh \
    <server-ip> [duration_s]

Defaults:
  duration_s=150
  rates=10M 15M 20M 40M 60M 80M 100M 120M

Optional environment:
  TRACE_SESSION_NAME  session directory name below traces/logs
  TRACE_LOG_ROOT      explicit local session directory; must be empty
  SERVER_SSH          SSH host/alias, default: sgp-server
  SERVER_REPO         remote repository, default: /root/Starlink_Measure
  ADB_HEAVY_DEVICE    Android serial for UDP uplink sender (required)
  ADB_ICMP_DEVICE     Android serial for ICMP RTT probe (required)
  UDP_RATES           whitespace-separated replacement rate list
  DRY_RUN=1           generate individual runner plans without postprocessing
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SERVER="${1:-${SERVER:-}}"
DURATION="${2:-150}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TRACE_SCRIPT_DIR="$REPO_DIR/traces/scripts"
SINGLE_RUNNER="$SCRIPT_DIR/run_dual_phone_icmp_heavy_udp.sh"
EXTRACTOR="$TRACE_SCRIPT_DIR/TraceDataProcess.py"
PLOTTER="$TRACE_SCRIPT_DIR/TraceVisual.py"
SERVER_SSH="${SERVER_SSH:-sgp-server}"
SERVER_REPO="${SERVER_REPO:-/root/Starlink_Measure}"
HEAVY_DEVICE="${ADB_HEAVY_DEVICE:-}"
ICMP_DEVICE="${ADB_ICMP_DEVICE:-}"
PORT="${UDP_PORT:-5201}"
UDP_LEN="${UDP_LEN:-1200}"
PING_INTERVAL="${PING_INTERVAL:-0.2}"
DRY_RUN="${DRY_RUN:-0}"
DEFAULT_RATES=(10M 15M 20M 40M 60M 80M 100M 120M)

fail() {
  echo "ERROR: $*" >&2
  exit 2
}

[[ -n "$SERVER" ]] || { usage >&2; fail "server IP/hostname is required"; }
[[ -n "$HEAVY_DEVICE" ]] || fail "ADB_HEAVY_DEVICE is required"
[[ -n "$ICMP_DEVICE" ]] || fail "ADB_ICMP_DEVICE is required"
[[ "$SERVER" =~ ^[A-Za-z0-9._:-]+$ ]] || fail "SERVER contains unsupported characters"
[[ "$DURATION" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "duration_s must be numeric"
awk -v d="$DURATION" 'BEGIN { exit !(d > 0) }' || fail "duration_s must be positive"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || fail "UDP_PORT must be in 1..65535"
[[ "$UDP_LEN" =~ ^[1-9][0-9]*$ ]] || fail "UDP_LEN must be a positive integer"
[[ "$PING_INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "PING_INTERVAL must be numeric"
awk -v i="$PING_INTERVAL" 'BEGIN { exit !(i > 0) }' || fail "PING_INTERVAL must be positive"
[[ "$DRY_RUN" == "0" || "$DRY_RUN" == "1" ]] || fail "DRY_RUN must be 0 or 1"
[[ "$HEAVY_DEVICE" != "$ICMP_DEVICE" ]] || fail "UDP and RTT devices must differ"

if [[ -n "${UDP_RATES:-}" ]]; then
  read -r -a RATES <<< "$UDP_RATES"
else
  RATES=("${DEFAULT_RATES[@]}")
fi
(( ${#RATES[@]} > 0 )) || fail "at least one UDP rate is required"
for rate in "${RATES[@]}"; do
  [[ "$rate" =~ ^[0-9]+([.][0-9]+)?[KMG]?$ ]] || fail "invalid UDP rate: $rate"
done

SESSION_NAME="${TRACE_SESSION_NAME:-role_swap_uplink_$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$SESSION_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || fail "TRACE_SESSION_NAME contains unsupported characters"
SESSION_DIR="${TRACE_LOG_ROOT:-$REPO_DIR/traces/logs/$SESSION_NAME}"
if [[ -e "$SESSION_DIR" ]]; then
  [[ -d "$SESSION_DIR" ]] || fail "TRACE_LOG_ROOT is not a directory: $SESSION_DIR"
  [[ -z "$(find "$SESSION_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] \
    || fail "TRACE_LOG_ROOT must be empty: $SESSION_DIR"
fi
mkdir -p "$SESSION_DIR"

for command in adb ssh python3; do
  command -v "$command" >/dev/null 2>&1 || fail "$command is required"
done
[[ -f "$SINGLE_RUNNER" ]] || fail "missing single-run runner: $SINGLE_RUNNER"
[[ -f "$EXTRACTOR" ]] || fail "missing extractor: $EXTRACTOR"
[[ -f "$PLOTTER" ]] || fail "missing plotter: $PLOTTER"

adb devices > "$SESSION_DIR/adb_devices_preflight.txt" 2>&1 || fail "failed to list ADB devices"
for device in "$HEAVY_DEVICE" "$ICMP_DEVICE"; do
  awk -v d="$device" '$1 == d && $2 == "device" { found=1 } END { exit !found }' \
    "$SESSION_DIR/adb_devices_preflight.txt" || fail "ADB device is not ready: $device"
done
adb -s "$HEAVY_DEVICE" shell "test -x /data/local/tmp/iperf3" \
  > "$SESSION_DIR/heavy_iperf3_preflight.stdout" \
  2> "$SESSION_DIR/heavy_iperf3_preflight.stderr" \
  || fail "iperf3 is not executable on UDP sender: $HEAVY_DEVICE"
adb -s "$HEAVY_DEVICE" shell "getprop ro.product.model; ip route; cmd wifi status 2>&1 || true" \
  > "$SESSION_DIR/heavy_phone_preflight.txt" 2>&1 || true
adb -s "$ICMP_DEVICE" shell "getprop ro.product.model; ip route; cmd wifi status 2>&1 || true" \
  > "$SESSION_DIR/icmp_phone_preflight.txt" 2>&1 || true
ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" \
  "command -v iperf3 >/dev/null && test -d '$SERVER_REPO'" \
  || fail "remote iperf3 or repository is unavailable"
PORT_LISTENER="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" \
  "ss -H -lun | awk '\$5 ~ /:$PORT\$/ { print; exit }'" 2>/dev/null || true)"
[[ -z "$PORT_LISTENER" ]] || fail "UDP port $PORT is already in use on the server: $PORT_LISTENER"

cat > "$SESSION_DIR/sweep_metadata.txt" <<META
session_name=$SESSION_NAME
server=$SERVER
server_ssh=$SERVER_SSH
server_repo=$SERVER_REPO
udp_direction=uplink
duration_s=$DURATION
udp_port=$PORT
udp_payload_len=$UDP_LEN
ping_interval_s=$PING_INTERVAL
icmp_device=$ICMP_DEVICE
heavy_udp_device=$HEAVY_DEVICE
rates=${RATES[*]}
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
META
printf 'rate\tstart_utc\tend_utc\texit_code\n' > "$SESSION_DIR/sweep_status.tsv"

FAILED=0
for rate in "${RATES[@]}"; do
  START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[$START_UTC] starting $rate uplink"
  set +e
  TRACE_LOG_ROOT="$SESSION_DIR" \
  ADB_HEAVY_DEVICE="$HEAVY_DEVICE" \
  ADB_ICMP_DEVICE="$ICMP_DEVICE" \
  SERVER_SSH="$SERVER_SSH" \
  SERVER_REPO="$SERVER_REPO" \
  UDP_DIRECTION=uplink \
  DRY_RUN="$DRY_RUN" \
  bash "$SINGLE_RUNNER" "$SERVER" "$DURATION" "$rate" "$PING_INTERVAL" "$PORT" "$UDP_LEN"
  RUN_RC=$?
  set -e
  END_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '%s\t%s\t%s\t%s\n' "$rate" "$START_UTC" "$END_UTC" "$RUN_RC" >> "$SESSION_DIR/sweep_status.tsv"
  if [[ "$RUN_RC" -ne 0 ]]; then
    FAILED=1
    echo "WARNING: $rate failed with exit code $RUN_RC; continuing" >&2
  fi
done

if [[ "$DRY_RUN" == "1" ]]; then
  echo "Dry run complete. Plans saved under: $SESSION_DIR"
  exit "$FAILED"
fi

set +e
python3 "$EXTRACTOR" --logs-dir "$SESSION_DIR"
EXTRACT_RC=$?
MPLBACKEND=Agg python3 "$PLOTTER" --logs-dir "$SESSION_DIR" --complete-only
PLOT_RC=$?
set -e

if [[ "$EXTRACT_RC" -ne 0 ]]; then
  FAILED=1
  echo "WARNING: extraction returned $EXTRACT_RC" >&2
fi
if [[ "$PLOT_RC" -ne 0 ]]; then
  FAILED=1
  echo "WARNING: plotting returned $PLOT_RC" >&2
fi

echo "Sweep outputs saved to: $SESSION_DIR"
exit "$FAILED"
