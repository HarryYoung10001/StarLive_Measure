#!/usr/bin/env bash
set -euo pipefail

# Run compact timestamped custom UDP downlink, optionally with ICMP.

usage() {
  cat <<'USAGE'
Usage:
  ADB_UDP_DEVICE=<serial> [ADB_ICMP_DEVICE=<serial>] SERVER_SSH=sgp-server \
    bash traces/scripts/_archived/run_custom_udp_latency_downlink.sh \
    [--no-icmp] <server-ip> [duration_s] [rate] [port] [payload_bytes] [ping_interval_s]

Defaults: duration_s=30 rate=200M port=5201 payload_bytes=1200 ping_interval_s=0.2
UDP_POSTPROCESS=arrival|latency selects a compact bandwidth or latency trace.
ICMP is enabled by default; --no-icmp runs with only the UDP phone.
USAGE
}

ICMP_ENABLED=1
POSITIONAL=()
while (($#)); do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --no-icmp) ICMP_ENABLED=0; shift ;;
    --) shift; POSITIONAL+=("$@"); break ;;
    --*) echo "ERROR: unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
(( ${#POSITIONAL[@]} <= 6 )) || { echo "ERROR: too many positional arguments" >&2; exit 2; }

SERVER="${POSITIONAL[0]-${SERVER:-}}"
DURATION="${POSITIONAL[1]-30}"
RATE="${POSITIONAL[2]-200M}"
PORT="${POSITIONAL[3]-${UDP_PORT:-5201}}"
PAYLOAD_BYTES="${POSITIONAL[4]-${UDP_PAYLOAD_BYTES:-1200}}"
PING_INTERVAL="${POSITIONAL[5]-${PING_INTERVAL:-0.2}}"
POSTPROCESS="${UDP_POSTPROCESS:-arrival}"
WINDOW_MS="${UDP_WINDOW_MS:-10}"
DURATION_MODE="${UDP_DURATION_MODE:-}"
if [[ -z "$DURATION_MODE" ]]; then
  if [[ "$POSTPROCESS" == "arrival" ]]; then DURATION_MODE=wall-clock; else DURATION_MODE=packet-count; fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TRACE_SCRIPT_DIR="$REPO_DIR/traces/scripts"
LOG_ROOT="${TRACE_LOG_ROOT:-$REPO_DIR/traces/logs}"
SERVER_SSH="${SERVER_SSH:-sgp-server}"
SERVER_REPO="${SERVER_REPO:-/root/Starlink_Measure}"
UDP_DEVICE="${ADB_UDP_DEVICE:-}"
ICMP_DEVICE="${ADB_ICMP_DEVICE:-}"
UDP_TIMEOUT_S="${UDP_TIMEOUT_S:-5}"
UDP_WAIT_TIMEOUT_S="${UDP_WAIT_TIMEOUT_S:-120}"
UDP_RECV_BUFFER_BYTES="${UDP_RECV_BUFFER_BYTES:-67108864}"
UDP_RECEIVE_API="${UDP_RECEIVE_API:-auto}"
UDP_RECEIVE_BATCH_SIZE="${UDP_RECEIVE_BATCH_SIZE:-64}"
UDP_MAX_RECEIVER_SOCKET_DROPS="${UDP_MAX_RECEIVER_SOCKET_DROPS:-0}"
PROGRESS_INTERVAL_S="${UDP_PROGRESS_INTERVAL_S:-1}"
MONITOR_INTERVAL_S="${UDP_MONITOR_INTERVAL_S:-2}"
GAP_MS="${UDP_GAP_MS:-100}"
DRY_RUN="${DRY_RUN:-0}"
DURATION_MS="$(awk -v d="$DURATION" 'BEGIN { printf "%d", d * 1000 + 0.5 }')"
DEFAULT_MIN_SPAN=$(( DURATION_MS > 1000 ? DURATION_MS - 1000 : 0 ))
DEFAULT_MAX_SPAN=$(( DURATION_MS + 5000 ))
MIN_RECV_SPAN_MS="${MIN_RECV_SPAN_MS:-$DEFAULT_MIN_SPAN}"
MAX_RECV_SPAN_MS="${MAX_RECV_SPAN_MS:-$DEFAULT_MAX_SPAN}"
MIN_SENDER_ELAPSED_S="${MIN_SENDER_ELAPSED_S:-$(awk -v d="$DURATION" 'BEGIN { v=d-1; if (v<0) v=0; printf "%.3f", v }')}"
MAX_SENDER_ELAPSED_S="${MAX_SENDER_ELAPSED_S:-$(awk -v d="$DURATION" 'BEGIN { printf "%.3f", d+2 }')}"

fail() {
  local message="$*"
  if [[ -n "${RUN_DIR:-}" ]]; then
    echo "ERROR: $message" | tee -a "$RUN_DIR/error.txt" >&2
    printf 'status=failed\nerror=%s\nend_utc=%s\n' "$message" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$RUN_DIR/metadata.txt"
    printf '2\n' > "$RUN_DIR/exit_code.txt"
  else
    echo "ERROR: $message" >&2
  fi
  exit 2
}

run_with_timeout() {
  local seconds="$1"
  shift
  "$@" &
  local child="$!" deadline=$(( $(date +%s) + seconds ))
  while kill -0 "$child" >/dev/null 2>&1; do
    if [[ "$(date +%s)" -ge "$deadline" ]]; then
      kill "$child" >/dev/null 2>&1 || true
      wait "$child" >/dev/null 2>&1 || true
      return 124
    fi
    sleep 1
  done
  wait "$child"
}

scp_retry() {
  local timeout="$1"
  shift
  local attempt
  for attempt in 1 2 3; do
    if run_with_timeout "$timeout" scp -q "$@"; then return 0; fi
    sleep 3
  done
  return 1
}

[[ -n "$SERVER" ]] || { usage >&2; fail "server IP/hostname is required"; }
if [[ "$DRY_RUN" == "0" ]]; then
  [[ -n "$UDP_DEVICE" ]] || fail "ADB_UDP_DEVICE is required"
  if (( ICMP_ENABLED )); then [[ -n "$ICMP_DEVICE" ]] || fail "ADB_ICMP_DEVICE is required unless --no-icmp is used"; fi
fi
[[ "$SERVER" =~ ^[A-Za-z0-9._:-]+$ ]] || fail "SERVER contains unsupported characters"
[[ "$SERVER_REPO" =~ ^[A-Za-z0-9._/-]+$ ]] || fail "SERVER_REPO contains unsupported characters"
[[ "$DURATION" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v v="$DURATION" 'BEGIN {exit !(v>0)}' || fail "duration_s must be positive"
[[ "$RATE" =~ ^[0-9]+([.][0-9]+)?([KMG]([bB][pP][sS])?|pps)?$ ]] || fail "invalid rate"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || fail "invalid port"
[[ "$PAYLOAD_BYTES" =~ ^[0-9]+$ ]] && (( PAYLOAD_BYTES >= 48 )) || fail "payload_bytes must be at least 48"
[[ "$POSTPROCESS" == "arrival" || "$POSTPROCESS" == "latency" ]] || fail "UDP_POSTPROCESS must be arrival or latency"
[[ "$DURATION_MODE" == "wall-clock" || "$DURATION_MODE" == "packet-count" ]] || fail "UDP_DURATION_MODE must be wall-clock or packet-count"
for value in "$PING_INTERVAL" "$UDP_TIMEOUT_S" "$UDP_WAIT_TIMEOUT_S" "$PROGRESS_INTERVAL_S" "$MONITOR_INTERVAL_S" "$GAP_MS" "$WINDOW_MS"; do
  [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "numeric configuration is invalid: $value"
done
[[ "$UDP_RECV_BUFFER_BYTES" =~ ^[0-9]+$ && "$UDP_RECV_BUFFER_BYTES" -ge 65536 ]] || fail "UDP_RECV_BUFFER_BYTES must be at least 65536"
[[ "$UDP_MAX_RECEIVER_SOCKET_DROPS" =~ ^[0-9]+$ ]] || fail "UDP_MAX_RECEIVER_SOCKET_DROPS must be a non-negative integer"
[[ "$UDP_RECEIVE_API" == "auto" || "$UDP_RECEIVE_API" == "readmsg" || "$UDP_RECEIVE_API" == "recvmmsg" ]] || fail "UDP_RECEIVE_API must be auto, readmsg, or recvmmsg"
[[ "$UDP_RECEIVE_BATCH_SIZE" =~ ^[1-9][0-9]*$ && "$UDP_RECEIVE_BATCH_SIZE" -le 256 ]] || fail "UDP_RECEIVE_BATCH_SIZE must be between 1 and 256"
awk -v w="$WINDOW_MS" -v p="$PROGRESS_INTERVAL_S" -v m="$MONITOR_INTERVAL_S" -v g="$GAP_MS" 'BEGIN {exit !(w>0 && p>0 && m>0 && g>0)}' || fail "window/progress/gap values must be positive"
[[ "$DRY_RUN" == "0" || "$DRY_RUN" == "1" ]] || fail "DRY_RUN must be 0 or 1"
if (( ICMP_ENABLED )); then [[ "$UDP_DEVICE" != "$ICMP_DEVICE" ]] || fail "UDP and ICMP devices must differ"; fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
SAFE_RATE="$(printf '%s' "$RATE" | tr -c 'A-Za-z0-9._-' '_')"
RUN_ID="dual_udp_latency_downlink_${TS}_${SAFE_RATE}_${DURATION}s"
RUN_DIR="$LOG_ROOT/$RUN_ID"
UDP_DIR="$RUN_DIR/udp_phone"
ICMP_DIR="$RUN_DIR/icmp_phone"
SERVER_DIR="$RUN_DIR/server"
REMOTE_ROOT="${REMOTE_ROOT:-/data/local/tmp/starlink_measure}"
PHONE_REMOTE_ROOT="$REMOTE_ROOT/$RUN_ID"
PHONE_UDP_DIR="$PHONE_REMOTE_ROOT/udp"
PHONE_ICMP_DIR="$PHONE_REMOTE_ROOT/icmp"
SERVER_REMOTE_DIR="$SERVER_REPO/traces/logs/$RUN_ID/server"
PING_COUNT=0
if (( ICMP_ENABLED )); then PING_COUNT="$(awk -v d="$DURATION" -v i="$PING_INTERVAL" 'BEGIN {printf "%d", int(d/i+0.999999)}')"; fi

mkdir -p "$UDP_DIR/remote" "$SERVER_DIR/remote"
if (( ICMP_ENABLED )); then mkdir -p "$ICMP_DIR/remote"; fi
cat > "$RUN_DIR/metadata.txt" <<META
run_id=$RUN_ID
status=starting
type=dual_udp_latency_downlink
server=$SERVER
server_ssh=$SERVER_SSH
server_repo=$SERVER_REPO
udp_direction=downlink
duration_s=$DURATION
udp_rate=$RATE
udp_payload_len=$PAYLOAD_BYTES
udp_port=$PORT
postprocess=$POSTPROCESS
window_ms=$WINDOW_MS
duration_mode=$DURATION_MODE
progress_interval_s=$PROGRESS_INTERVAL_S
monitor_interval_s=$MONITOR_INTERVAL_S
gap_ms=$GAP_MS
icmp_enabled=$([[ "$ICMP_ENABLED" -eq 1 ]] && echo true || echo false)
ping_interval_s=$([[ "$ICMP_ENABLED" -eq 1 ]] && echo "$PING_INTERVAL" || echo disabled)
ping_count=$PING_COUNT
udp_timeout_s=$UDP_TIMEOUT_S
udp_wait_timeout_s=$UDP_WAIT_TIMEOUT_S
receive_api=$UDP_RECEIVE_API
receive_api_requested=$UDP_RECEIVE_API
receive_capture_bytes=256
receive_batch_size=$UDP_RECEIVE_BATCH_SIZE
udp_device=$UDP_DEVICE
icmp_device=$([[ "$ICMP_ENABLED" -eq 1 ]] && echo "$ICMP_DEVICE" || echo disabled)
protocol=DWLP_v2
clock_sync_samples_required=5
clock_timestamp_fields=server_send_unix_ns,phone_recv_unix_ns
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
META

cat > "$RUN_DIR/server_runner.sh" <<'SERVER_RUNNER'
#!/usr/bin/env bash
set -u
REMOTE_DIR="$1"; PORT="$2"; DURATION="$3"; RATE="$4"; PAYLOAD="$5"; RUN_ID="$6"; MODE="$7"; WAIT_TIMEOUT="$8"; PROGRESS_INTERVAL="$9"
mkdir -p "$REMOTE_DIR"
python3 "$REMOTE_DIR/downlink_udp_sender.py" --port "$PORT" --duration-s "$DURATION" --rate "$RATE" --payload-bytes "$PAYLOAD" --run-id "$RUN_ID" --out-dir "$REMOTE_DIR" --duration-mode "$MODE" --no-csv --wait-timeout-s "$WAIT_TIMEOUT" --clock-samples 5 --ready-file "$REMOTE_DIR/ready" --progress-path "$REMOTE_DIR/downlink_udp_sender_progress.tsv" --progress-interval-s "$PROGRESS_INTERVAL" > "$REMOTE_DIR/downlink_udp_sender.stdout" 2> "$REMOTE_DIR/downlink_udp_sender.stderr"
RC="$?"
printf '%s\n' "$RC" > "$REMOTE_DIR/exit_code.txt"
exit "$RC"
SERVER_RUNNER

cat > "$RUN_DIR/udp_runner.sh" <<'UDP_RUNNER'
#!/system/bin/sh
set -u
DIR="$1"; BIN="$2"; RUN_ID="$3"; SERVER="$4"; PORT="$5"; DURATION="$6"; PAYLOAD="$7"; TIMEOUT="$8"; WAIT_TIMEOUT="$9"; MODE="${10}"; WINDOW="${11}"; PROGRESS="${12}"; GAP="${13}"; RECV_BUFFER="${14}"; RECEIVE_API="${15}"; RECEIVE_BATCH_SIZE="${16}"
mkdir -p "$DIR"
"$BIN" --run-id "$RUN_ID" --server "$SERVER" --port "$PORT" --duration-s "$DURATION" --timeout-s "$TIMEOUT" --wait-timeout-s "$WAIT_TIMEOUT" --payload-bytes "$PAYLOAD" --out-dir "$DIR" --output-mode "$MODE" --window-ms "$WINDOW" --progress-interval "${PROGRESS}s" --gap-ms "$GAP" --recv-buffer-bytes "$RECV_BUFFER" --receive-api "$RECEIVE_API" --receive-batch-size "$RECEIVE_BATCH_SIZE" > "$DIR/downlink_udp_receiver.stdout" 2> "$DIR/downlink_udp_receiver.stderr"
RC="$?"
printf '%s\n' "$RC" > "$DIR/downlink_udp_receiver_exit_code.txt"
exit "$RC"
UDP_RUNNER

if (( ICMP_ENABLED )); then
cat > "$RUN_DIR/icmp_runner.sh" <<'ICMP_RUNNER'
#!/system/bin/sh
set -u
DIR="$1"; SERVER="$2"; INTERVAL="$3"; COUNT="$4"
mkdir -p "$DIR"
ping -D -i "$INTERVAL" -c "$COUNT" "$SERVER" > "$DIR/ping.log" 2> "$DIR/ping.stderr"
RC="$?"
printf '%s\n' "$RC" > "$DIR/exit_code.txt"
exit "$RC"
ICMP_RUNNER
fi
chmod +x "$RUN_DIR/server_runner.sh" "$RUN_DIR/udp_runner.sh"
if (( ICMP_ENABLED )); then chmod +x "$RUN_DIR/icmp_runner.sh"; fi

if [[ "$DRY_RUN" == "1" ]]; then
  {
    printf 'server_command=ssh %s bash server_runner.sh %s %s %s %s %s %s %s %s\n' "$SERVER_SSH" "$SERVER_REMOTE_DIR" "$PORT" "$DURATION" "$RATE" "$PAYLOAD_BYTES" "$RUN_ID" "$DURATION_MODE" "$UDP_WAIT_TIMEOUT_S"
    printf 'udp_command=adb -s %s shell downlink_udp_receiver --output-mode %s --window-ms %s\n' "$UDP_DEVICE" "$POSTPROCESS" "$WINDOW_MS"
    if (( ICMP_ENABLED )); then printf 'icmp_command=adb -s %s shell ping %s\n' "$ICMP_DEVICE" "$SERVER"; else printf 'icmp_status=skipped\n'; fi
  } > "$RUN_DIR/plan.txt"
  printf 'status=planned\n' >> "$RUN_DIR/metadata.txt"
  echo "Dry run plan: $RUN_DIR/plan.txt"
  exit 0
fi

for command in adb ssh scp go python3 gzip tar; do command -v "$command" >/dev/null || fail "$command is required"; done
adb devices > "$RUN_DIR/adb_devices_preflight.txt" 2>&1 || fail "failed to list ADB devices"
awk -v d="$UDP_DEVICE" '$1==d && $2=="device" {ok=1} END {exit !ok}' "$RUN_DIR/adb_devices_preflight.txt" || fail "ADB UDP device is not ready: $UDP_DEVICE"
if (( ICMP_ENABLED )); then awk -v d="$ICMP_DEVICE" '$1==d && $2=="device" {ok=1} END {exit !ok}' "$RUN_DIR/adb_devices_preflight.txt" || fail "ADB ICMP device is not ready: $ICMP_DEVICE"; fi
UDP_ABI_LIST="$(adb -s "$UDP_DEVICE" shell getprop ro.product.cpu.abilist 2>/dev/null | tr -d '\r')"
UDP_BOOT_COMPLETED="$(adb -s "$UDP_DEVICE" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')"
[[ "$UDP_ABI_LIST" == *arm* && "$UDP_BOOT_COMPLETED" == "1" ]] || fail "UDP phone shell is not ready"

if [[ ",$UDP_ABI_LIST," == *,arm64-v8a,* ]]; then
  RECEIVER_GOOS=android; RECEIVER_GOARCH=arm64; RECEIVER_TAG=arm64; RECEIVER_GOARM=""
elif [[ ",$UDP_ABI_LIST," == *,armeabi-v7a,* ]]; then
  RECEIVER_GOOS=linux; RECEIVER_GOARCH=arm; RECEIVER_TAG=armv7; RECEIVER_GOARM=7
else
  fail "unsupported UDP phone ABI: $UDP_ABI_LIST"
fi
RECEIVER_BIN="$RUN_DIR/downlink_udp_receiver_${RECEIVER_TAG}"
if [[ -n "$RECEIVER_GOARM" ]]; then
  GOOS="$RECEIVER_GOOS" GOARCH="$RECEIVER_GOARCH" GOARM="$RECEIVER_GOARM" CGO_ENABLED=0 go build -trimpath -ldflags '-s -w' -o "$RECEIVER_BIN" "$REPO_DIR/mac_reemo/downlink_udp_receiver.go" > "$UDP_DIR/go_build.stdout" 2> "$UDP_DIR/go_build.stderr"
else
  GOOS="$RECEIVER_GOOS" GOARCH="$RECEIVER_GOARCH" CGO_ENABLED=0 go build -trimpath -ldflags '-s -w' -o "$RECEIVER_BIN" "$REPO_DIR/mac_reemo/downlink_udp_receiver.go" > "$UDP_DIR/go_build.stdout" 2> "$UDP_DIR/go_build.stderr"
fi

cleanup_failed_server() {
  local status="$?"
  if [[ -n "${MONITOR_PID:-}" ]]; then kill "$MONITOR_PID" >/dev/null 2>&1 || true; wait "$MONITOR_PID" >/dev/null 2>&1 || true; fi
  if [[ "$status" -ne 0 ]]; then
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "for pid in \$(ps -eo pid=,args= | awk -v id='$RUN_ID' '\$0 ~ /downlink_udp_sender.py/ && index(\$0,id) {print \$1}'); do kill -TERM \"\$pid\" 2>/dev/null || true; done" >/dev/null 2>&1 || true
  fi
  return "$status"
}
trap cleanup_failed_server EXIT

ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" "mkdir -p '$SERVER_REMOTE_DIR' && rm -f '$SERVER_REMOTE_DIR/ready'"
scp_retry 30 "$REPO_DIR/server_receiver/scripts/downlink_udp_sender.py" "$SERVER_SSH:$SERVER_REMOTE_DIR/downlink_udp_sender.py" > "$SERVER_DIR/scp_source.stdout" 2> "$SERVER_DIR/scp_source.stderr" || fail "failed to copy downlink sender"
adb -s "$UDP_DEVICE" shell "mkdir -p '$PHONE_UDP_DIR'"
adb -s "$UDP_DEVICE" push "$RECEIVER_BIN" /data/local/tmp/downlink_udp_receiver > "$UDP_DIR/adb_push_receiver.stdout" 2> "$UDP_DIR/adb_push_receiver.stderr"
adb -s "$UDP_DEVICE" shell chmod 755 /data/local/tmp/downlink_udp_receiver
adb -s "$UDP_DEVICE" shell /data/local/tmp/downlink_udp_receiver --version > "$UDP_DIR/receiver_version.stdout" 2> "$UDP_DIR/receiver_version.stderr"
adb -s "$UDP_DEVICE" push "$RUN_DIR/udp_runner.sh" "$PHONE_UDP_DIR/runner.sh" > "$UDP_DIR/adb_push_runner.stdout" 2> "$UDP_DIR/adb_push_runner.stderr"
if (( ICMP_ENABLED )); then
  adb -s "$ICMP_DEVICE" shell "mkdir -p '$PHONE_ICMP_DIR'"
  adb -s "$ICMP_DEVICE" push "$RUN_DIR/icmp_runner.sh" "$PHONE_ICMP_DIR/runner.sh" > "$ICMP_DIR/adb_push_runner.stdout" 2> "$ICMP_DIR/adb_push_runner.stderr"
fi

ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" "bash -s -- '$SERVER_REMOTE_DIR' '$PORT' '$DURATION' '$RATE' '$PAYLOAD_BYTES' '$RUN_ID' '$DURATION_MODE' '$UDP_WAIT_TIMEOUT_S' '$PROGRESS_INTERVAL_S'" < "$RUN_DIR/server_runner.sh" > "$SERVER_DIR/ssh.stdout" 2> "$SERVER_DIR/ssh.stderr" &
SERVER_PID=$!
for _ in $(seq 1 60); do
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "test -f '$SERVER_REMOTE_DIR/ready'" && break
  sleep 1
done
ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "test -f '$SERVER_REMOTE_DIR/ready'" || fail "server sender did not become ready"

if (( ICMP_ENABLED )); then
  adb -s "$ICMP_DEVICE" shell "sh '$PHONE_ICMP_DIR/runner.sh' '$PHONE_ICMP_DIR' '$SERVER' '$PING_INTERVAL' '$PING_COUNT'" > "$ICMP_DIR/adb_runner.stdout" 2> "$ICMP_DIR/adb_runner.stderr" &
  ICMP_PID=$!
else
  ICMP_PID=""; ICMP_RC=0; ICMP_PULL_RC=0
fi
adb -s "$UDP_DEVICE" shell "sh '$PHONE_UDP_DIR/runner.sh' '$PHONE_UDP_DIR' /data/local/tmp/downlink_udp_receiver '$RUN_ID' '$SERVER' '$PORT' '$DURATION' '$PAYLOAD_BYTES' '$UDP_TIMEOUT_S' '$UDP_WAIT_TIMEOUT_S' '$POSTPROCESS' '$WINDOW_MS' '$PROGRESS_INTERVAL_S' '$GAP_MS' '$UDP_RECV_BUFFER_BYTES' '$UDP_RECEIVE_API' '$UDP_RECEIVE_BATCH_SIZE'" > "$UDP_DIR/adb_runner.stdout" 2> "$UDP_DIR/adb_runner.stderr" &
UDP_PID=$!

MONITOR_DURATION_S="$(awk -v d="$DURATION" -v t="$UDP_TIMEOUT_S" 'BEGIN {printf "%.3f", d+t+3}')"
    python3 "$TRACE_SCRIPT_DIR/MonitorUDPProgress.py" --direction downlink --adb-device "$UDP_DEVICE" --phone-path "$PHONE_UDP_DIR/downlink_udp_receiver_progress.tsv" --server-ssh "$SERVER_SSH" --server-path "$SERVER_REMOTE_DIR/downlink_udp_sender_progress.tsv" --output "$RUN_DIR/udp_progress_monitor.tsv" --interval-s "$MONITOR_INTERVAL_S" --duration-s "$MONITOR_DURATION_S" --command-timeout-s 10 > "$RUN_DIR/udp_progress_monitor.stdout" 2> "$RUN_DIR/udp_progress_monitor.stderr" &
MONITOR_PID=$!

set +e
wait "$UDP_PID"; UDP_RC=$?
if (( ICMP_ENABLED )); then wait "$ICMP_PID"; ICMP_RC=$?; fi
wait "$SERVER_PID"; SERVER_RC=$?
wait "$MONITOR_PID" >/dev/null 2>&1; MONITOR_RC=$?
set -e
printf '%s\n' "$UDP_RC" > "$UDP_DIR/adb_exit_code.txt"
printf '%s\n' "$SERVER_RC" > "$SERVER_DIR/ssh_exit_code.txt"
if (( ICMP_ENABLED )); then printf '%s\n' "$ICMP_RC" > "$ICMP_DIR/adb_exit_code.txt"; fi

PHONE_FILES=(downlink_udp_receiver_summary.txt downlink_udp_receiver_progress.tsv udp_receive_gap_report.tsv downlink_udp_receiver.stdout downlink_udp_receiver.stderr downlink_udp_receiver_exit_code.txt)
if [[ "$POSTPROCESS" == "arrival" ]]; then PHONE_FILES+=(udp_arrival_time_ms.txt); else PHONE_FILES+=(delay.txt delay_window_time_ms.txt delay_summary.txt); fi
UDP_PULL_RC=0
for name in "${PHONE_FILES[@]}"; do
  run_with_timeout 60 adb -s "$UDP_DEVICE" pull "$PHONE_UDP_DIR/$name" "$UDP_DIR/remote/" >> "$UDP_DIR/adb_pull.stdout" 2>> "$UDP_DIR/adb_pull.stderr" || UDP_PULL_RC=1
done
SERVER_PULL_RC=0
for name in downlink_udp_sender_summary.txt downlink_udp_sender_progress.tsv downlink_udp_control.csv metadata.txt downlink_udp_sender.stdout downlink_udp_sender.stderr exit_code.txt; do
  scp_retry 30 "$SERVER_SSH:$SERVER_REMOTE_DIR/$name" "$SERVER_DIR/remote/" >> "$SERVER_DIR/scp.stdout" 2>> "$SERVER_DIR/scp.stderr" || SERVER_PULL_RC=1
done
if (( ICMP_ENABLED )); then
  run_with_timeout 120 adb -s "$ICMP_DEVICE" pull "$PHONE_ICMP_DIR/." "$ICMP_DIR/remote" > "$ICMP_DIR/adb_pull.stdout" 2> "$ICMP_DIR/adb_pull.stderr" || ICMP_PULL_RC=1
  if [[ -s "$ICMP_DIR/remote/ping.log" ]]; then
    python3 "$TRACE_SCRIPT_DIR/ICMPPingSummaryProcess.py" "$ICMP_DIR/remote/ping.log" --expected-transmitted "$PING_COUNT" --output "$ICMP_DIR/remote/ping_summary.txt" > "$ICMP_DIR/ping_summary.stdout" 2> "$ICMP_DIR/ping_summary.stderr" || ICMP_PULL_RC=1
  else ICMP_PULL_RC=1; fi
fi

cp "$UDP_DIR/remote/downlink_udp_receiver_summary.txt" "$UDP_DIR/downlink_udp_receiver_summary.txt" 2>/dev/null || true
cp "$SERVER_DIR/remote/downlink_udp_sender_summary.txt" "$SERVER_DIR/downlink_udp_sender_summary.txt" 2>/dev/null || true
cp "$UDP_DIR/remote/udp_receive_gap_report.tsv" "$UDP_DIR/udp_receive_gap_report.tsv" 2>/dev/null || true
if [[ "$POSTPROCESS" == "arrival" && -s "$UDP_DIR/remote/udp_arrival_time_ms.txt" ]]; then
  cp "$UDP_DIR/remote/udp_arrival_time_ms.txt" "$RUN_DIR/udp_arrival_time_ms.txt"
  gzip -9 -c "$RUN_DIR/udp_arrival_time_ms.txt" > "$RUN_DIR/udp_arrival_time_ms.txt.gz"
  python3 "$TRACE_SCRIPT_DIR/BandwidthTimeScatter.py" "$RUN_DIR" --window-ms "$WINDOW_MS" --duration-ms "$DURATION_MS" --output "$RUN_DIR/bandwidth_time_scatter.png" > "$RUN_DIR/bandwidth_plot.stdout" 2> "$RUN_DIR/bandwidth_plot.stderr" || true
fi
if [[ "$POSTPROCESS" == "latency" && -s "$UDP_DIR/remote/delay.txt" ]]; then
  cp "$UDP_DIR/remote/delay.txt" "$RUN_DIR/delay.txt"
  cp "$UDP_DIR/remote/delay_window_time_ms.txt" "$RUN_DIR/delay_window_time_ms.txt"
  cp "$UDP_DIR/remote/delay_summary.txt" "$RUN_DIR/delay_summary.txt"
  gzip -9 -c "$RUN_DIR/delay.txt" > "$RUN_DIR/delay.txt.gz"
  python3 "$TRACE_SCRIPT_DIR/DelayTimeScatter.py" "$RUN_DIR" > "$RUN_DIR/delay_plot.stdout" 2> "$RUN_DIR/delay_plot.stderr" || true
fi

PROGRESS_AUDIT_RC=1
if [[ -s "$UDP_DIR/udp_receive_gap_report.tsv" && -s "$SERVER_DIR/remote/downlink_udp_sender_progress.tsv" && -s "$UDP_DIR/remote/downlink_udp_receiver_progress.tsv" && -s "$UDP_DIR/downlink_udp_receiver_summary.txt" ]]; then
  python3 "$TRACE_SCRIPT_DIR/UDPProgressAudit.py" classify "$UDP_DIR/udp_receive_gap_report.tsv" --direction downlink --sender-progress "$SERVER_DIR/remote/downlink_udp_sender_progress.tsv" --receiver-progress "$UDP_DIR/remote/downlink_udp_receiver_progress.tsv" --clock-summary "$UDP_DIR/downlink_udp_receiver_summary.txt" --output "$RUN_DIR/udp_receive_gap_audit.tsv" > "$RUN_DIR/udp_receive_gap_audit.stdout" 2> "$RUN_DIR/udp_receive_gap_audit.stderr" && PROGRESS_AUDIT_RC=0
fi

EXPECTED_PACKETS="$(awk -v rate="$RATE" -v duration="$DURATION" -v payload="$PAYLOAD_BYTES" '
BEGIN { v=tolower(rate); suffix="bps"; if(v~/gbps$/){suffix="g";sub(/gbps$/,"",v)} else if(v~/mbps$/){suffix="m";sub(/mbps$/,"",v)} else if(v~/kbps$/){suffix="k";sub(/kbps$/,"",v)} else if(v~/pps$/){suffix="pps";sub(/pps$/,"",v)} else if(v~/g$/){suffix="g";sub(/g$/,"",v)} else if(v~/m$/){suffix="m";sub(/m$/,"",v)} else if(v~/k$/){suffix="k";sub(/k$/,"",v)} pps=v+0; if(suffix=="g")pps=pps*1e9/(payload*8); else if(suffix=="m")pps=pps*1e6/(payload*8); else if(suffix=="k")pps=pps*1e3/(payload*8); else if(suffix=="bps")pps=pps/(payload*8); printf "%d", int(duration*pps+0.999999999) }')"
kv() { awk -F= -v key="$2" '$1==key {print $2; exit}' "$1" 2>/dev/null || true; }
SENDER_SUMMARY="$SERVER_DIR/downlink_udp_sender_summary.txt"
RECEIVER_SUMMARY="$UDP_DIR/downlink_udp_receiver_summary.txt"
SCHEDULED_PACKETS="$(kv "$SENDER_SUMMARY" scheduled_packets)"
ATTEMPTED_PACKETS="$(kv "$SENDER_SUMMARY" attempted_packets)"
SENT_PACKETS="$(kv "$SENDER_SUMMARY" sent_packets)"
SEND_FAILED_PACKETS="$(kv "$SENDER_SUMMARY" send_failed_packets)"
SENDER_ELAPSED_S="$(kv "$SENDER_SUMMARY" sender_elapsed_s)"
ACTUAL_PAYLOAD_MBPS="$(kv "$SENDER_SUMMARY" actual_payload_mbps)"
DEADLINE_REACHED="$(kv "$SENDER_SUMMARY" deadline_reached)"
DONE_ACK="$(kv "$SENDER_SUMMARY" done_ack)"
RECEIVED_PACKETS="$(kv "$RECEIVER_SUMMARY" received_packets)"
CLOCK_SYNC_SAMPLES="$(kv "$RECEIVER_SUMMARY" clock_sync_samples)"
LAST_RECV_REL_S="$(kv "$RECEIVER_SUMMARY" last_recv_rel_s)"
PAYLOAD_MIN="$(kv "$RECEIVER_SUMMARY" payload_bytes_min)"
PAYLOAD_MAX="$(kv "$RECEIVER_SUMMARY" payload_bytes_max)"
RECEIVER_INTEGRITY="$(kv "$RECEIVER_SUMMARY" receiver_integrity)"
SOCKET_RXQ_DROPS="$(kv "$RECEIVER_SUMMARY" socket_rxq_overflow_packets)"
CAPTURE_BACKPRESSURE="$(kv "$RECEIVER_SUMMARY" capture_backpressure_records)"
RECEIVE_API_REQUESTED="$(kv "$RECEIVER_SUMMARY" receive_api_requested)"
RECEIVE_API_EFFECTIVE="$(kv "$RECEIVER_SUMMARY" receive_api_effective)"
HEADER_ONLY_PACKETS="$(kv "$RECEIVER_SUMMARY" header_only_packets)"
CONTROL_TRUNCATED_PACKETS="$(kv "$RECEIVER_SUMMARY" control_truncated_packets)"
RECV_SPAN_MS="$(awk -v s="$LAST_RECV_REL_S" 'BEGIN {if(s!="") printf "%.3f", s*1000}')"
AVERAGE_BANDWIDTH_MBPS="$(awk -v n="$RECEIVED_PACKETS" -v p="$PAYLOAD_BYTES" -v s="$LAST_RECV_REL_S" 'BEGIN {if(n>0 && s>0) printf "%.9f", n*p*8/s/1e6}')"

FINAL_RC=0
for rc in "$UDP_RC" "$SERVER_RC" "$UDP_PULL_RC" "$SERVER_PULL_RC" "$MONITOR_RC" "$PROGRESS_AUDIT_RC"; do [[ "$rc" -eq 0 ]] || FINAL_RC=1; done
[[ -s "$RUN_DIR/udp_progress_monitor.tsv" ]] || FINAL_RC=1
[[ "$SCHEDULED_PACKETS" =~ ^[0-9]+$ && "$SCHEDULED_PACKETS" -eq "$EXPECTED_PACKETS" ]] || FINAL_RC=1
[[ "$ATTEMPTED_PACKETS" =~ ^[1-9][0-9]*$ && "$SENT_PACKETS" =~ ^[1-9][0-9]*$ ]] || FINAL_RC=1
[[ "$SEND_FAILED_PACKETS" =~ ^[0-9]+$ && "$SEND_FAILED_PACKETS" -eq 0 ]] || FINAL_RC=1
[[ "$DONE_ACK" == "true" ]] || FINAL_RC=1
[[ "$RECEIVED_PACKETS" =~ ^[1-9][0-9]*$ ]] || FINAL_RC=1
[[ "$CLOCK_SYNC_SAMPLES" == "5" ]] || FINAL_RC=1
[[ "$PAYLOAD_MIN" == "$PAYLOAD_BYTES" && "$PAYLOAD_MAX" == "$PAYLOAD_BYTES" ]] || FINAL_RC=1
[[ "$RECEIVER_INTEGRITY" == "clean" ]] || FINAL_RC=1
[[ "$SOCKET_RXQ_DROPS" =~ ^[0-9]+$ && "$SOCKET_RXQ_DROPS" -le "$UDP_MAX_RECEIVER_SOCKET_DROPS" ]] || FINAL_RC=1
[[ "$CAPTURE_BACKPRESSURE" == "0" ]] || FINAL_RC=1
awk -v span="$RECV_SPAN_MS" -v min="$MIN_RECV_SPAN_MS" -v max="$MAX_RECV_SPAN_MS" 'BEGIN {exit !(span>=min && span<=max)}' || FINAL_RC=1
if [[ "$POSTPROCESS" == "arrival" ]]; then
  ARRIVAL_ROWS="$(wc -l < "$RUN_DIR/udp_arrival_time_ms.txt" 2>/dev/null | tr -d ' ' || true)"
  [[ "$DURATION_MODE" == "wall-clock" && "$DEADLINE_REACHED" == "true" ]] || FINAL_RC=1
  awk -v e="$SENDER_ELAPSED_S" -v min="$MIN_SENDER_ELAPSED_S" -v max="$MAX_SENDER_ELAPSED_S" 'BEGIN {exit !(e>=min && e<=max)}' || FINAL_RC=1
  [[ "$ARRIVAL_ROWS" == "$RECEIVED_PACKETS" && -s "$RUN_DIR/udp_arrival_time_ms.txt.gz" && -s "$RUN_DIR/bandwidth_time_scatter.png" ]] || FINAL_RC=1
  awk '$0!~/^[0-9]+$/ || (NR>1 && $1<prev){bad=1}{prev=$1}END{exit bad}' "$RUN_DIR/udp_arrival_time_ms.txt" || FINAL_RC=1
  DELAY_ROWS=0; TIME_ROWS=0; EMPTY_WINDOWS=""; DELAY_VALID_ROWS=0; DELAY_WINDOWS=0; DELAY_SKIPPED_ROWS=0
else
  DELAY_ROWS="$(wc -l < "$RUN_DIR/delay.txt" 2>/dev/null | tr -d ' ' || true)"
  TIME_ROWS="$(wc -l < "$RUN_DIR/delay_window_time_ms.txt" 2>/dev/null | tr -d ' ' || true)"
  EMPTY_WINDOWS="$(kv "$RUN_DIR/delay_summary.txt" empty_windows)"
  DELAY_VALID_ROWS="$(kv "$RUN_DIR/delay_summary.txt" valid_delay_rows)"
  DELAY_WINDOWS="$(kv "$RUN_DIR/delay_summary.txt" delay_windows)"
  DELAY_SKIPPED_ROWS="$(kv "$RUN_DIR/delay_summary.txt" skipped_rows)"
  [[ "$DURATION_MODE" == "packet-count" && "$SENT_PACKETS" -eq "$EXPECTED_PACKETS" ]] || FINAL_RC=1
  [[ "$DELAY_ROWS" =~ ^[1-9][0-9]*$ && "$TIME_ROWS" == "$DELAY_ROWS" && -s "$RUN_DIR/delay.txt.gz" && -s "$RUN_DIR/time_delay_scatter.png" ]] || FINAL_RC=1
  [[ "$DELAY_WINDOWS" == "$TIME_ROWS" && "$DELAY_VALID_ROWS" == "$RECEIVED_PACKETS" && "$DELAY_SKIPPED_ROWS" == "0" ]] || FINAL_RC=1
  ARRIVAL_ROWS=0
fi
if (( ICMP_ENABLED )); then
  [[ "$ICMP_RC" -eq 0 && "$ICMP_PULL_RC" -eq 0 ]] || FINAL_RC=1
  ICMP_STATUS="$([[ "$ICMP_RC" -eq 0 && "$ICMP_PULL_RC" -eq 0 ]] && echo complete || echo failed)"
else ICMP_STATUS=skipped; fi

cat > "$RUN_DIR/exit_status.txt" <<STATUS
udp_rc=$UDP_RC
server_rc=$SERVER_RC
udp_pull_rc=$UDP_PULL_RC
server_pull_rc=$SERVER_PULL_RC
monitor_rc=$MONITOR_RC
progress_audit_rc=$PROGRESS_AUDIT_RC
duration_mode=$DURATION_MODE
scheduled_packets=$SCHEDULED_PACKETS
attempted_packets=$ATTEMPTED_PACKETS
sent_packets=$SENT_PACKETS
send_failed_packets=$SEND_FAILED_PACKETS
sender_elapsed_s=$SENDER_ELAPSED_S
actual_payload_mbps=$ACTUAL_PAYLOAD_MBPS
deadline_reached=$DEADLINE_REACHED
received_packets=$RECEIVED_PACKETS
arrival_rows=$ARRIVAL_ROWS
delay_rows=$DELAY_ROWS
time_rows=$TIME_ROWS
delay_empty_windows=$EMPTY_WINDOWS
delay_valid_rows=$DELAY_VALID_ROWS
delay_windows=$DELAY_WINDOWS
delay_skipped_rows=$DELAY_SKIPPED_ROWS
recv_span_ms=$RECV_SPAN_MS
clock_sync_samples=$CLOCK_SYNC_SAMPLES
average_bandwidth_mbps=$AVERAGE_BANDWIDTH_MBPS
receiver_integrity=$RECEIVER_INTEGRITY
socket_rxq_overflow_packets=$SOCKET_RXQ_DROPS
capture_backpressure_records=$CAPTURE_BACKPRESSURE
requested_rcvbuf_bytes=$UDP_RECV_BUFFER_BYTES
receive_api=$RECEIVE_API_EFFECTIVE
receive_api_requested=$RECEIVE_API_REQUESTED
receive_api_effective=$RECEIVE_API_EFFECTIVE
receive_capture_bytes=$(kv "$RECEIVER_SUMMARY" receive_capture_bytes)
header_only_packets=$HEADER_ONLY_PACKETS
control_truncated_packets=$CONTROL_TRUNCATED_PACKETS
receive_batch_size=$UDP_RECEIVE_BATCH_SIZE
icmp_status=$ICMP_STATUS
STATUS
printf 'average_bandwidth_mbps=%s\nsender_progress_file=server/remote/downlink_udp_sender_progress.tsv\nreceiver_progress_file=udp_phone/remote/downlink_udp_receiver_progress.tsv\ngap_report_file=udp_phone/udp_receive_gap_report.tsv\ngap_audit_file=udp_receive_gap_audit.tsv\n' "$AVERAGE_BANDWIDTH_MBPS" >> "$RUN_DIR/metadata.txt"

adb -s "$UDP_DEVICE" shell "rm -rf '$PHONE_REMOTE_ROOT'" >/dev/null 2>&1 || true
if (( ICMP_ENABLED )); then adb -s "$ICMP_DEVICE" shell "rm -rf '$PHONE_REMOTE_ROOT'" >/dev/null 2>&1 || true; fi
if [[ "$FINAL_RC" -eq 0 ]]; then
  echo 'status=complete' >> "$RUN_DIR/metadata.txt"
  echo "icmp_status=$ICMP_STATUS" >> "$RUN_DIR/metadata.txt"
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "rm -rf '$SERVER_REMOTE_DIR'" >/dev/null 2>&1 || true
else
  echo 'status=failed' >> "$RUN_DIR/metadata.txt"
  echo "icmp_status=$ICMP_STATUS" >> "$RUN_DIR/metadata.txt"
  echo "remote_server_dir=$SERVER_REMOTE_DIR" >> "$RUN_DIR/metadata.txt"
fi
printf '%s\n' "$FINAL_RC" > "$RUN_DIR/exit_code.txt"
echo "Custom UDP downlink logs: $RUN_DIR"
exit "$FINAL_RC"
