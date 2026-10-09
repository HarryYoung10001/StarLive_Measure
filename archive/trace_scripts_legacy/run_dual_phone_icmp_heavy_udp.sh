#!/usr/bin/env bash
set -euo pipefail

# Run an ICMP stream on one Android phone while a separate phone carries a
# heavy UDP flow. The server-side iperf3 process is started through SSH.
#
# Usage:
#   ADB_ICMP_DEVICE=<serial> ADB_HEAVY_DEVICE=<serial> \
#   SERVER_SSH=sgp-server \
#     bash traces/scripts/_archived/run_dual_phone_icmp_heavy_udp.sh \
#       <server-ip> [duration_s] [udp_rate] [ping_interval_s] [port] [udp_len]
#
# Defaults:
#   duration_s=150, udp_rate=350M, ping_interval_s=0.2, port=5201, udp_len=1200
#   UDP_DIRECTION=downlink (server -> heavy phone)
#
# Set DRY_RUN=1 to write the planned commands and metadata without requiring
# adb or SSH.

usage() {
  cat <<'USAGE'
Usage:
  ADB_ICMP_DEVICE=<serial> ADB_HEAVY_DEVICE=<serial> \
  SERVER_SSH=sgp-server bash traces/scripts/_archived/run_dual_phone_icmp_heavy_udp.sh \
    <server-ip> [duration_s] [udp_rate] [ping_interval_s] [port] [udp_len]

Environment:
  ADB_ICMP_DEVICE       serial for the phone running ICMP (required unless DRY_RUN=1)
  ADB_HEAVY_DEVICE      serial for the phone running heavy UDP (required unless DRY_RUN=1)
  SERVER_SSH             SSH host/alias, default: sgp-server
  SERVER_REPO             remote repository, default: /root/Starlink_Measure
  UDP_DIRECTION          downlink or uplink, default: downlink
  IPERF_BIN              Android iperf3 path, default: /data/local/tmp/iperf3
  REMOTE_ROOT            Android working directory, default: /data/local/tmp/starlink_measure
  SERVER_READY_TIMEOUT_S seconds to wait for the remote server, default: 30
  SERVER_READY_WAIT_S    server startup settle time, default: 1
  CAPACITY_INTERVAL_S    server receiver report interval, default: ping interval
  SERVER_FINISH_TIMEOUT_S seconds to wait after phone runners return, default: duration + 60
  PHONE_FINISH_TIMEOUT_S seconds to wait for both Android runners, default: duration + 60
  DRY_RUN=1              plan only; do not call adb or SSH
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SERVER="${1:-${SERVER:-}}"
DURATION="${2:-150}"
UDP_RATE="${3:-350M}"
PING_INTERVAL="${4:-0.2}"
PORT="${5:-5201}"
UDP_LEN="${6:-1200}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
LOG_ROOT="${TRACE_LOG_ROOT:-$REPO_DIR/traces/logs}"
SERVER_SSH="${SERVER_SSH:-sgp-server}"
SERVER_REPO="${SERVER_REPO:-/root/Starlink_Measure}"
UDP_DIRECTION="${UDP_DIRECTION:-downlink}"
IPERF_BIN="${IPERF_BIN:-/data/local/tmp/iperf3}"
REMOTE_ROOT="${REMOTE_ROOT:-/data/local/tmp/starlink_measure}"
SERVER_READY_TIMEOUT_S="${SERVER_READY_TIMEOUT_S:-30}"
SERVER_READY_WAIT_S="${SERVER_READY_WAIT_S:-1}"
CAPACITY_INTERVAL_S="${CAPACITY_INTERVAL_S:-$PING_INTERVAL}"
DRY_RUN="${DRY_RUN:-0}"
ICMP_DEVICE="${ADB_ICMP_DEVICE:-${ICMP_DEVICE:-}}"
HEAVY_DEVICE="${ADB_HEAVY_DEVICE:-${HEAVY_DEVICE:-}}"

fail() {
  local message="$*"
  if [[ -n "${RUN_DIR:-}" ]]; then
    echo "ERROR: $message" | tee -a "$RUN_DIR/error.txt" >&2
    if [[ -f "$RUN_DIR/metadata.txt" ]]; then
      printf 'status=failed\nerror=%s\nend_utc=%s\n' \
        "$message" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$RUN_DIR/metadata.txt"
      printf '2\n' > "$RUN_DIR/exit_code.txt"
      ln -sfn "$RUN_DIR" "$LOG_ROOT/latest_dual_icmp_heavy_udp"
    fi
  else
    echo "ERROR: $message" >&2
  fi
  exit 2
}

[[ -n "$SERVER" ]] || { usage >&2; fail "server IP/hostname is required"; }
[[ "$SERVER" =~ ^[A-Za-z0-9._:-]+$ ]] || fail "SERVER contains unsupported characters: $SERVER"
[[ "$UDP_DIRECTION" == "downlink" || "$UDP_DIRECTION" == "uplink" ]] || fail "UDP_DIRECTION must be downlink or uplink"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || fail "PORT must be in 1..65535"
[[ "$UDP_LEN" =~ ^[1-9][0-9]*$ ]] || fail "udp_len must be a positive integer"
[[ "$DURATION" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "duration_s must be numeric"
awk -v d="$DURATION" 'BEGIN { exit !(d > 0) }' || fail "duration_s must be positive"
[[ "$PING_INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "ping_interval_s must be numeric"
awk -v i="$PING_INTERVAL" 'BEGIN { exit !(i > 0) }' || fail "ping_interval_s must be positive"
[[ "$SERVER_READY_TIMEOUT_S" =~ ^[1-9][0-9]*$ ]] || fail "SERVER_READY_TIMEOUT_S must be a positive integer"
[[ "$SERVER_READY_WAIT_S" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "SERVER_READY_WAIT_S must be numeric"
awk -v w="$SERVER_READY_WAIT_S" 'BEGIN { exit !(w >= 0) }' || fail "SERVER_READY_WAIT_S must be non-negative"
[[ "$CAPACITY_INTERVAL_S" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "CAPACITY_INTERVAL_S must be numeric"
awk -v i="$CAPACITY_INTERVAL_S" 'BEGIN { exit !(i > 0) }' || fail "CAPACITY_INTERVAL_S must be positive"
[[ "$UDP_RATE" =~ ^[0-9]+([.][0-9]+)?[KMG]?$ ]] || fail "udp_rate must look like 350M, 1G, or 500K"
RATE_VALUE="${UDP_RATE%[KMG]}"
awk -v r="$RATE_VALUE" 'BEGIN { exit !(r > 0) }' || fail "udp_rate must be positive"
[[ "$DRY_RUN" == "0" || "$DRY_RUN" == "1" ]] || fail "DRY_RUN must be 0 or 1"
[[ "$IPERF_BIN" =~ ^[A-Za-z0-9._/-]+$ ]] || fail "IPERF_BIN contains unsupported characters"
[[ "$REMOTE_ROOT" =~ ^[A-Za-z0-9._/-]+$ ]] || fail "REMOTE_ROOT contains unsupported characters"
[[ "$SERVER_REPO" =~ ^[A-Za-z0-9._/-]+$ ]] || fail "SERVER_REPO contains unsupported characters"

PING_COUNT="$(awk -v d="$DURATION" -v i="$PING_INTERVAL" 'BEGIN { printf "%d", int(d / i + 0.999999) }')"
DURATION_CEIL="$(awk -v d="$DURATION" 'BEGIN { printf "%d", int(d + 0.999999) }')"
SERVER_FINISH_TIMEOUT_S="${SERVER_FINISH_TIMEOUT_S:-$((DURATION_CEIL + 60))}"
[[ "$SERVER_FINISH_TIMEOUT_S" =~ ^[1-9][0-9]*$ ]] || fail "SERVER_FINISH_TIMEOUT_S must be a positive integer"
PHONE_FINISH_TIMEOUT_S="${PHONE_FINISH_TIMEOUT_S:-$((DURATION_CEIL + 60))}"
[[ "$PHONE_FINISH_TIMEOUT_S" =~ ^[1-9][0-9]*$ ]] || fail "PHONE_FINISH_TIMEOUT_S must be a positive integer"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
SAFE_RATE="$(printf '%s' "$UDP_RATE" | tr -c 'A-Za-z0-9._-' '_')"
RUN_ID="dual_icmp_heavy_udp_${TS}_${SAFE_RATE}_${DURATION}s"
RUN_DIR="$LOG_ROOT/$RUN_ID"
ICMP_DIR="$RUN_DIR/icmp_phone"
HEAVY_DIR="$RUN_DIR/heavy_udp_phone"
SERVER_DIR="$RUN_DIR/server"
REMOTE_RUN_ID="${RUN_ID}"
SERVER_REMOTE_DIR="$SERVER_REPO/traces/logs/$REMOTE_RUN_ID/server"
ICMP_REMOTE_DIR="$REMOTE_ROOT/$REMOTE_RUN_ID/icmp"
HEAVY_REMOTE_DIR="$REMOTE_ROOT/$REMOTE_RUN_ID/heavy_udp"

mkdir -p "$ICMP_DIR" "$HEAVY_DIR" "$SERVER_DIR"

cat > "$RUN_DIR/metadata.txt" <<META
run_id=$RUN_ID
status=starting
type=dual_phone_icmp_heavy_udp
server=$SERVER
server_ssh=$SERVER_SSH
server_repo=$SERVER_REPO
udp_direction=$UDP_DIRECTION
duration_s=$DURATION
udp_rate=$UDP_RATE
udp_payload_len=$UDP_LEN
udp_port=$PORT
ping_interval_s=$PING_INTERVAL
ping_count=$PING_COUNT
server_finish_timeout_s=$SERVER_FINISH_TIMEOUT_S
phone_finish_timeout_s=$PHONE_FINISH_TIMEOUT_S
capacity_interval_s=$CAPACITY_INTERVAL_S
icmp_device=${ICMP_DEVICE:-unset}
heavy_udp_device=${HEAVY_DEVICE:-unset}
iperf_bin=$IPERF_BIN
icmp_remote_dir=$ICMP_REMOTE_DIR
heavy_udp_remote_dir=$HEAVY_REMOTE_DIR
server_remote_dir=$SERVER_REMOTE_DIR
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
mac_hostname=$(hostname)
note=two independent Android devices; compare ICMP RTT/loss against heavy UDP goodput/loss
META

cat > "$RUN_DIR/server_runner.sh" <<'REMOTE_SERVER_RUNNER'
#!/usr/bin/env bash
set -u

REMOTE_DIR="$1"
PORT="$2"
READY_WAIT_S="$3"
INTERVAL="$4"

mkdir -p "$REMOTE_DIR"
date -u +%Y-%m-%dT%H:%M:%SZ > "$REMOTE_DIR/server_start_time_utc.txt"
if ! command -v iperf3 >/dev/null 2>&1; then
  echo "iperf3 not found" > "$REMOTE_DIR/iperf3_server.stderr"
  echo 127 > "$REMOTE_DIR/exit_code.txt"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$REMOTE_DIR/server_end_time_utc.txt"
  exit 127
fi

iperf3 -s -p "$PORT" -1 -i "$INTERVAL" -J --forceflush \
  > "$REMOTE_DIR/iperf3_server.json" \
  2> "$REMOTE_DIR/iperf3_server.stderr" &
IPERF_PID="$!"
printf '%s\n' "$IPERF_PID" > "$REMOTE_DIR/iperf3.pid"
sleep "$READY_WAIT_S"
if kill -0 "$IPERF_PID" >/dev/null 2>&1; then
  touch "$REMOTE_DIR/ready"
else
  echo "iperf3 exited before ready" >> "$REMOTE_DIR/iperf3_server.stderr"
  wait "$IPERF_PID" 2>/dev/null
  RC="$?"
  printf '%s\n' "$RC" > "$REMOTE_DIR/exit_code.txt"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$REMOTE_DIR/server_end_time_utc.txt"
  exit "$RC"
fi

wait "$IPERF_PID"
RC="$?"
printf '%s\n' "$RC" > "$REMOTE_DIR/exit_code.txt"
date -u +%Y-%m-%dT%H:%M:%SZ > "$REMOTE_DIR/server_end_time_utc.txt"
exit "$RC"
REMOTE_SERVER_RUNNER

cat > "$RUN_DIR/icmp_runner.sh" <<'REMOTE_ICMP_RUNNER'
#!/system/bin/sh
set -u

REMOTE_DIR="$1"
SERVER="$2"
PING_INTERVAL="$3"
PING_COUNT="$4"

mkdir -p "$REMOTE_DIR"
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$REMOTE_DIR/android_start_time_utc.txt" 2>/dev/null || date > "$REMOTE_DIR/android_start_time.txt"
printf 'ping -D -i %s -c %s %s\n' "$PING_INTERVAL" "$PING_COUNT" "$SERVER" > "$REMOTE_DIR/command.txt"
ping -D -i "$PING_INTERVAL" -c "$PING_COUNT" "$SERVER" \
  > "$REMOTE_DIR/ping.log" 2> "$REMOTE_DIR/ping.stderr"
RC="$?"
printf '%s\n' "$RC" > "$REMOTE_DIR/exit_code.txt"
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$REMOTE_DIR/android_end_time_utc.txt" 2>/dev/null || date > "$REMOTE_DIR/android_end_time.txt"
exit "$RC"
REMOTE_ICMP_RUNNER

cat > "$RUN_DIR/heavy_runner.sh" <<'REMOTE_HEAVY_RUNNER'
#!/system/bin/sh
set -u

REMOTE_DIR="$1"
SERVER="$2"
PORT="$3"
RATE="$4"
DURATION="$5"
INTERVAL="$6"
LEN="$7"
IPERF_BIN="$8"
DIRECTION="$9"
CAPACITY_INTERVAL="$10"

mkdir -p "$REMOTE_DIR"
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$REMOTE_DIR/android_start_time_utc.txt" 2>/dev/null || date > "$REMOTE_DIR/android_start_time.txt"
if [ "$DIRECTION" = "downlink" ]; then
  CMD="$IPERF_BIN -c $SERVER -p $PORT -u -R -b $RATE -l $LEN -t $DURATION -i $CAPACITY_INTERVAL -J --get-server-output"
  printf '%s\n' "$CMD" > "$REMOTE_DIR/command.txt"
  "$IPERF_BIN" -c "$SERVER" -p "$PORT" -u -R -b "$RATE" -l "$LEN" -t "$DURATION" -i "$CAPACITY_INTERVAL" -J --get-server-output \
    > "$REMOTE_DIR/iperf3_client.json" 2> "$REMOTE_DIR/iperf3_client.stderr"
else
  CMD="$IPERF_BIN -c $SERVER -p $PORT -u -b $RATE -l $LEN -t $DURATION -i $CAPACITY_INTERVAL -J --get-server-output"
  printf '%s\n' "$CMD" > "$REMOTE_DIR/command.txt"
  "$IPERF_BIN" -c "$SERVER" -p "$PORT" -u -b "$RATE" -l "$LEN" -t "$DURATION" -i "$CAPACITY_INTERVAL" -J --get-server-output \
    > "$REMOTE_DIR/iperf3_client.json" 2> "$REMOTE_DIR/iperf3_client.stderr"
fi
RC="$?"
printf '%s\n' "$RC" > "$REMOTE_DIR/exit_code.txt"
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$REMOTE_DIR/android_end_time_utc.txt" 2>/dev/null || date > "$REMOTE_DIR/android_end_time.txt"
exit "$RC"
REMOTE_HEAVY_RUNNER

chmod +x "$RUN_DIR"/{server_runner.sh,icmp_runner.sh,heavy_runner.sh}

if [[ "$DRY_RUN" == "1" ]]; then
  cat > "$RUN_DIR/plan.txt" <<PLAN
server_command=ssh $SERVER_SSH bash -s -- $SERVER_REMOTE_DIR $PORT $SERVER_READY_WAIT_S $CAPACITY_INTERVAL_S
icmp_command=adb -s ${ICMP_DEVICE:-<icmp-device>} shell sh $ICMP_REMOTE_DIR/runner.sh $ICMP_REMOTE_DIR $SERVER $PING_INTERVAL $PING_COUNT
heavy_command=adb -s ${HEAVY_DEVICE:-<heavy-device>} shell sh $HEAVY_REMOTE_DIR/runner.sh $HEAVY_REMOTE_DIR $SERVER $PORT $UDP_RATE $DURATION $PING_INTERVAL $UDP_LEN $IPERF_BIN $UDP_DIRECTION $CAPACITY_INTERVAL_S
PLAN
  printf 'status=planned\n' >> "$RUN_DIR/metadata.txt"
  ln -sfn "$RUN_DIR" "$LOG_ROOT/latest_dual_icmp_heavy_udp"
  echo "Dry run complete. Plan and metadata saved to: $RUN_DIR"
  exit 0
fi

command -v adb >/dev/null 2>&1 || fail "adb not found"
command -v ssh >/dev/null 2>&1 || fail "ssh not found"
command -v scp >/dev/null 2>&1 || fail "scp not found"
[[ -n "$ICMP_DEVICE" ]] || fail "set ADB_ICMP_DEVICE to the ICMP phone serial"
[[ -n "$HEAVY_DEVICE" ]] || fail "set ADB_HEAVY_DEVICE to the heavy UDP phone serial"
[[ "$ICMP_DEVICE" != "$HEAVY_DEVICE" ]] || fail "ICMP and heavy UDP devices must be different"

adb devices > "$RUN_DIR/adb_devices.txt" 2>&1 || fail "failed to list adb devices"
for device in "$ICMP_DEVICE" "$HEAVY_DEVICE"; do
  awk -v d="$device" '$1 == d && $2 == "device" { found=1 } END { exit !found }' "$RUN_DIR/adb_devices.txt" \
    || fail "ADB device is not ready: $device"
done

collect_phone_info() {
  local device="$1" output_dir="$2"
  adb -s "$device" shell "uname -a; getprop ro.product.model; getprop ro.product.cpu.abi; getprop ro.product.cpu.abilist; getprop ro.build.version.sdk; date; ip route; cmd wifi status 2>/dev/null || true" \
    > "$output_dir/phone_info.txt" 2>&1 || true
}
collect_phone_info "$ICMP_DEVICE" "$ICMP_DIR"
collect_phone_info "$HEAVY_DEVICE" "$HEAVY_DIR"
adb -s "$HEAVY_DEVICE" shell "test -x '$IPERF_BIN'" \
  > "$HEAVY_DIR/iperf_precheck.stdout" 2> "$HEAVY_DIR/iperf_precheck.stderr" \
  || fail "iperf3 is not executable on heavy UDP device: $IPERF_BIN"

adb -s "$ICMP_DEVICE" shell "rm -rf '$ICMP_REMOTE_DIR'; mkdir -p '$ICMP_REMOTE_DIR'" \
  > "$ICMP_DIR/remote_mkdir.stdout" 2> "$ICMP_DIR/remote_mkdir.stderr"
adb -s "$HEAVY_DEVICE" shell "rm -rf '$HEAVY_REMOTE_DIR'; mkdir -p '$HEAVY_REMOTE_DIR'" \
  > "$HEAVY_DIR/remote_mkdir.stdout" 2> "$HEAVY_DIR/remote_mkdir.stderr"
adb -s "$ICMP_DEVICE" push "$RUN_DIR/icmp_runner.sh" "$ICMP_REMOTE_DIR/runner.sh" \
  > "$ICMP_DIR/adb_push.stdout" 2> "$ICMP_DIR/adb_push.stderr"
adb -s "$HEAVY_DEVICE" push "$RUN_DIR/heavy_runner.sh" "$HEAVY_REMOTE_DIR/runner.sh" \
  > "$HEAVY_DIR/adb_push.stdout" 2> "$HEAVY_DIR/adb_push.stderr"

SERVER_PID=""
ICMP_PID=""
HEAVY_PID=""
ICMP_PULL_RC=0
HEAVY_PULL_RC=0
SERVER_SCP_RC=0
FINALIZED=0

cat > "$SERVER_DIR/command.txt" <<CMD
ssh $SERVER_SSH bash -s -- $SERVER_REMOTE_DIR $PORT $SERVER_READY_WAIT_S $CAPACITY_INTERVAL_S
CMD

cleanup_remote() {
  adb -s "$ICMP_DEVICE" shell "rm -rf '$ICMP_REMOTE_DIR'" >/dev/null 2>&1 || true
  adb -s "$HEAVY_DEVICE" shell "rm -rf '$HEAVY_REMOTE_DIR'" >/dev/null 2>&1 || true
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "if [ ! -f '$SERVER_REMOTE_DIR/exit_code.txt' ] && [ -f '$SERVER_REMOTE_DIR/iperf3.pid' ]; then kill \$(cat '$SERVER_REMOTE_DIR/iperf3.pid') 2>/dev/null || true; fi" >/dev/null 2>&1 || true
}

stop_pid() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill "$pid" >/dev/null 2>&1 || true
  wait "$pid" >/dev/null 2>&1 || true
}

finalize() {
  local requested_rc="$1" icmp_rc heavy_rc server_rc final_rc
  [[ "$FINALIZED" -eq 0 ]] || return
  FINALIZED=1
  stop_pid "$ICMP_PID"
  stop_pid "$HEAVY_PID"
  stop_pid "$SERVER_PID"
  cleanup_remote

  icmp_rc="$(cat "$ICMP_DIR/remote/exit_code.txt" 2>/dev/null || cat "$ICMP_DIR/adb_exit_code.txt" 2>/dev/null || printf '%s' "$requested_rc")"
  heavy_rc="$(cat "$HEAVY_DIR/remote/exit_code.txt" 2>/dev/null || cat "$HEAVY_DIR/adb_exit_code.txt" 2>/dev/null || printf '%s' "$requested_rc")"
  server_rc="$(cat "$SERVER_DIR/exit_code.txt" 2>/dev/null || printf '%s' "$requested_rc")"
  printf '%s\n' "$icmp_rc" > "$RUN_DIR/icmp_exit_code.txt"
  printf '%s\n' "$heavy_rc" > "$RUN_DIR/heavy_udp_exit_code.txt"
  printf '%s\n' "$server_rc" > "$RUN_DIR/server_exit_code.txt"
  if [[ "$requested_rc" -ne 0 || "$icmp_rc" -ne 0 || "$heavy_rc" -ne 0 || "$server_rc" -ne 0 ]]; then
    final_rc="${requested_rc:-1}"
    [[ "$final_rc" -eq 0 ]] && final_rc=1
    printf 'status=failed\n' >> "$RUN_DIR/metadata.txt"
  else
    final_rc=0
    printf 'status=complete\n' >> "$RUN_DIR/metadata.txt"
  fi
  printf 'icmp_exit_code=%s\nheavy_udp_exit_code=%s\nserver_exit_code=%s\nicmp_pull_exit_code=%s\nheavy_udp_pull_exit_code=%s\nserver_scp_exit_code=%s\nend_utc=%s\n' \
    "$icmp_rc" "$heavy_rc" "$server_rc" "$ICMP_PULL_RC" "$HEAVY_PULL_RC" "$SERVER_SCP_RC" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$RUN_DIR/metadata.txt"
  printf '%s\n' "$final_rc" > "$RUN_DIR/exit_code.txt"
  ln -sfn "$RUN_DIR" "$LOG_ROOT/latest_dual_icmp_heavy_udp"
}

on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ "$rc" -ne 0 || "$FINALIZED" -eq 0 ]]; then
    finalize "$rc"
  fi
  exit "$rc"
}

on_signal() {
  cleanup_remote
  exit "$1"
}

trap on_exit EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

echo "[server] starting iperf3 via $SERVER_SSH"
set +e
ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" \
  "bash -s -- '$SERVER_REMOTE_DIR' '$PORT' '$SERVER_READY_WAIT_S' '$CAPACITY_INTERVAL_S'" \
  < "$RUN_DIR/server_runner.sh" \
  > "$SERVER_DIR/ssh.stdout" 2> "$SERVER_DIR/ssh.stderr" &
SERVER_PID="$!"
set -e

deadline=$(( $(date +%s) + ${SERVER_READY_TIMEOUT_S%.*} ))
while :; do
  if ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "test -f '$SERVER_REMOTE_DIR/ready'" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    fail "remote iperf3 server exited before becoming ready; see $SERVER_DIR/ssh.stderr"
  fi
  [[ "$(date +%s)" -lt "$deadline" ]] || fail "timed out waiting for remote iperf3 server readiness"
  sleep 1
done

EXPERIMENT_START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'experiment_start_utc=%s\nserver_ready_utc=%s\n' \
  "$EXPERIMENT_START_UTC" "$EXPERIMENT_START_UTC" >> "$RUN_DIR/metadata.txt"

echo "[phones] starting ICMP and heavy UDP concurrently"
set +e
PHONES_START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'phones_start_utc=%s\n' "$PHONES_START_UTC" >> "$RUN_DIR/metadata.txt"
adb -s "$ICMP_DEVICE" shell "sh '$ICMP_REMOTE_DIR/runner.sh' '$ICMP_REMOTE_DIR' '$SERVER' '$PING_INTERVAL' '$PING_COUNT'" \
  > "$ICMP_DIR/adb_runner.stdout" 2> "$ICMP_DIR/adb_runner.stderr" &
ICMP_PID="$!"
adb -s "$HEAVY_DEVICE" shell "sh '$HEAVY_REMOTE_DIR/runner.sh' '$HEAVY_REMOTE_DIR' '$SERVER' '$PORT' '$UDP_RATE' '$DURATION' '$PING_INTERVAL' '$UDP_LEN' '$IPERF_BIN' '$UDP_DIRECTION' '$CAPACITY_INTERVAL_S'" \
  > "$HEAVY_DIR/adb_runner.stdout" 2> "$HEAVY_DIR/adb_runner.stderr" &
HEAVY_PID="$!"
set -e

ICMP_WAIT_RC=""
HEAVY_WAIT_RC=""
phone_finish_deadline=$(( $(date +%s) + PHONE_FINISH_TIMEOUT_S ))
while [[ -z "$ICMP_WAIT_RC" || -z "$HEAVY_WAIT_RC" ]]; do
  if [[ -z "$ICMP_WAIT_RC" ]] && ! kill -0 "$ICMP_PID" >/dev/null 2>&1; then
    set +e
    wait "$ICMP_PID"
    ICMP_WAIT_RC="$?"
    set -e
  fi
  if [[ -z "$HEAVY_WAIT_RC" ]] && ! kill -0 "$HEAVY_PID" >/dev/null 2>&1; then
    set +e
    wait "$HEAVY_PID"
    HEAVY_WAIT_RC="$?"
    set -e
  fi
  if [[ -n "$ICMP_WAIT_RC" && -n "$HEAVY_WAIT_RC" ]]; then
    break
  fi
  if [[ "$(date +%s)" -ge "$phone_finish_deadline" ]]; then
    if [[ -z "$ICMP_WAIT_RC" ]]; then
      stop_pid "$ICMP_PID"
      ICMP_WAIT_RC=124
    fi
    if [[ -z "$HEAVY_WAIT_RC" ]]; then
      stop_pid "$HEAVY_PID"
      HEAVY_WAIT_RC=124
    fi
    break
  fi
  sleep 1
done
printf '%s\n' "$ICMP_WAIT_RC" > "$ICMP_DIR/adb_exit_code.txt"
printf '%s\n' "$HEAVY_WAIT_RC" > "$HEAVY_DIR/adb_exit_code.txt"

mkdir -p "$ICMP_DIR/remote" "$HEAVY_DIR/remote"
set +e
adb -s "$ICMP_DEVICE" pull "$ICMP_REMOTE_DIR/." "$ICMP_DIR/remote" \
  > "$ICMP_DIR/adb_pull.stdout" 2> "$ICMP_DIR/adb_pull.stderr"
ICMP_PULL_RC="$?"
adb -s "$HEAVY_DEVICE" pull "$HEAVY_REMOTE_DIR/." "$HEAVY_DIR/remote" \
  > "$HEAVY_DIR/adb_pull.stdout" 2> "$HEAVY_DIR/adb_pull.stderr"
HEAVY_PULL_RC="$?"
set -e
printf '%s\n' "$ICMP_PULL_RC" > "$ICMP_DIR/adb_pull_exit_code.txt"
printf '%s\n' "$HEAVY_PULL_RC" > "$HEAVY_DIR/adb_pull_exit_code.txt"

echo "[server] waiting for remote iperf3 completion"
finish_deadline=$(( $(date +%s) + SERVER_FINISH_TIMEOUT_S ))
while kill -0 "$SERVER_PID" >/dev/null 2>&1; do
  if [[ "$(date +%s)" -ge "$finish_deadline" ]]; then
    fail "timed out waiting for remote iperf3 completion"
  fi
  sleep 1
done
set +e
wait "$SERVER_PID"
SERVER_WAIT_RC="$?"
set -e
printf '%s\n' "$SERVER_WAIT_RC" > "$SERVER_DIR/ssh_exit_code.txt"
mkdir -p "$SERVER_DIR/remote"
set +e
scp -q -r "$SERVER_SSH:$SERVER_REMOTE_DIR/." "$SERVER_DIR/remote/" \
  > "$SERVER_DIR/scp.stdout" 2> "$SERVER_DIR/scp.stderr"
SERVER_SCP_RC="$?"
set -e
printf '%s\n' "$SERVER_SCP_RC" > "$SERVER_DIR/scp_exit_code.txt"
if [[ -f "$SERVER_DIR/remote/iperf3_server.json" ]]; then
  cp "$SERVER_DIR/remote/iperf3_server.json" "$SERVER_DIR/iperf3_server.json"
  cp "$SERVER_DIR/remote/iperf3_server.stderr" "$SERVER_DIR/iperf3_server.stderr" 2>/dev/null || true
  cp "$SERVER_DIR/remote/exit_code.txt" "$SERVER_DIR/exit_code.txt" 2>/dev/null || true
fi

UPLINK_SERVER_JSON_VALID=1
if [[ "$UDP_DIRECTION" == "uplink" ]]; then
  UPLINK_SERVER_JSON_VALID=0
  if ! command -v python3 >/dev/null 2>&1; then
    echo "WARNING: python3 is required to validate uplink server JSON" >&2
  elif [[ ! -s "$SERVER_DIR/iperf3_server.json" ]]; then
    echo "WARNING: uplink server JSON is missing or empty" >&2
  elif python3 - "$SERVER_DIR/iperf3_server.json" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, json.JSONDecodeError) as exc:
    print(f"invalid uplink server JSON: {exc}", file=sys.stderr)
    raise SystemExit(1)

intervals = data.get("intervals")
valid = False
if isinstance(intervals, list):
    for interval in intervals:
        summary = interval.get("sum") if isinstance(interval, dict) else None
        if not isinstance(summary, dict):
            continue
        if summary.get("sender") is not False:
            continue
        if isinstance(summary.get("bits_per_second"), (int, float)):
            valid = True
            break

if not valid:
    print("uplink server JSON has no sender=false receiver interval", file=sys.stderr)
    raise SystemExit(1)
PY
  then
    UPLINK_SERVER_JSON_VALID=1
  else
    echo "WARNING: uplink server JSON has no usable receiver intervals" >&2
  fi
  printf 'server_json_valid=%s\n' "$UPLINK_SERVER_JSON_VALID" >> "$RUN_DIR/metadata.txt"
fi

FINAL_RC=0
[[ "$ICMP_WAIT_RC" -eq 0 ]] || FINAL_RC=1
[[ "$HEAVY_WAIT_RC" -eq 0 ]] || FINAL_RC=1
[[ "$ICMP_PULL_RC" -eq 0 ]] || FINAL_RC=1
[[ "$HEAVY_PULL_RC" -eq 0 ]] || FINAL_RC=1
[[ "$SERVER_WAIT_RC" -eq 0 ]] || FINAL_RC=1
[[ "$SERVER_SCP_RC" -eq 0 ]] || FINAL_RC=1
[[ -s "$SERVER_DIR/iperf3_server.json" ]] || FINAL_RC=1
[[ "$UPLINK_SERVER_JSON_VALID" -eq 1 ]] || FINAL_RC=1
finalize "$FINAL_RC"
trap - EXIT INT TERM

if [[ "$FINAL_RC" -ne 0 ]]; then
  echo "WARNING: dual-phone experiment failed (icmp=$ICMP_WAIT_RC/$ICMP_PULL_RC heavy=$HEAVY_WAIT_RC/$HEAVY_PULL_RC server=$SERVER_WAIT_RC/$SERVER_SCP_RC)"
  echo "Logs saved to: $RUN_DIR"
  exit "$FINAL_RC"
fi

echo "Done. Logs saved to: $RUN_DIR"
