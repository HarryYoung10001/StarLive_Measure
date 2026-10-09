#!/usr/bin/env bash
set -euo pipefail

# Run timestamped custom UDP uplink, optionally with ICMP on another phone.

usage() {
  cat <<'USAGE'
Usage:
  ADB_UDP_DEVICE=<serial> [ADB_ICMP_DEVICE=<serial>] SERVER_SSH=sgp-server \
    bash traces/scripts/_archived/run_custom_udp_latency_uplink.sh \
    [--no-icmp] [server-ip] [duration_s] [rate] [port] [payload_bytes] [ping_interval_s]

Defaults: duration_s=150 rate=100M port=5201 payload_bytes=1200 ping_interval_s=0.2
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
(( ${#POSITIONAL[@]} <= 6 )) || { echo "ERROR: too many positional arguments" >&2; usage >&2; exit 2; }

SERVER="${POSITIONAL[0]-${SERVER:-}}"
DURATION="${POSITIONAL[1]-150}"
RATE="${POSITIONAL[2]-100M}"
PORT="${POSITIONAL[3]-${UDP_PORT:-5201}}"
PAYLOAD_BYTES="${POSITIONAL[4]-${UDP_PAYLOAD_BYTES:-1200}}"
PING_INTERVAL="${POSITIONAL[5]-${PING_INTERVAL:-0.2}}"
POSTPROCESS="${UDP_POSTPROCESS:-arrival}"
WINDOW_MS="${UDP_WINDOW_MS:-10}"
UDP_RECEIVE_API="${UDP_RECEIVE_API:-auto}"
UDP_RECEIVE_BATCH_SIZE="${UDP_RECEIVE_BATCH_SIZE:-64}"
UDP_MAX_RECEIVER_SOCKET_DROPS="${UDP_MAX_RECEIVER_SOCKET_DROPS:-0}"
UDP_REQUIRE_RECEIVE_API="${UDP_REQUIRE_RECEIVE_API:-}"
EXPECTED_WIFI_SSID="${EXPECTED_WIFI_SSID:-}"
EXPECTED_DEVICE_MODEL="${EXPECTED_DEVICE_MODEL:-}"
MIN_RECV_SPAN_MS="${MIN_RECV_SPAN_MS:-149000}"
MAX_RECV_SPAN_MS="${MAX_RECV_SPAN_MS:-155000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TRACE_SCRIPT_DIR="$REPO_DIR/traces/scripts"
LOG_ROOT="${TRACE_LOG_ROOT:-$REPO_DIR/traces/logs}"
SERVER_SSH="${SERVER_SSH:-sgp-server}"
SERVER_REPO="${SERVER_REPO:-/root/Starlink_Measure}"
UDP_DEVICE="${ADB_UDP_DEVICE:-}"
ICMP_DEVICE="${ADB_ICMP_DEVICE:-}"
UDP_TIMEOUT_S="${UDP_TIMEOUT_S:-5}"
UDP_RECV_BUFFER_BYTES="${UDP_RECV_BUFFER_BYTES:-67108864}"
PROGRESS_INTERVAL_S="${UDP_PROGRESS_INTERVAL_S:-1}"
MONITOR_INTERVAL_S="${UDP_MONITOR_INTERVAL_S:-2}"
GAP_MS="${UDP_GAP_MS:-100}"
MIN_ARRIVAL_MAX_MS="${MIN_ARRIVAL_MAX_MS:-149000}"
MAX_ARRIVAL_MAX_MS="${MAX_ARRIVAL_MAX_MS:-155000}"
DURATION_MODE="${UDP_DURATION_MODE:-packet-count}"
if [[ "$POSTPROCESS" == "arrival" ]]; then
  DURATION_MODE="${UDP_DURATION_MODE:-wall-clock}"
fi
MIN_SENDER_ELAPSED_S="${MIN_SENDER_ELAPSED_S:-149}"
MAX_SENDER_ELAPSED_S="${MAX_SENDER_ELAPSED_S:-152}"

fail() { echo "ERROR: $*" >&2; exit 2; }

run_with_timeout() {
  local seconds="$1"
  shift
  "$@" &
  local child="$!"
  local deadline=$(( $(date +%s) + seconds ))
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
    if run_with_timeout "$timeout" scp -q "$@"; then
      return 0
    fi
    sleep 5
  done
  return 1
}
[[ -n "$UDP_DEVICE" ]] || fail "ADB_UDP_DEVICE is required"
if (( ICMP_ENABLED )); then
  [[ -n "$ICMP_DEVICE" ]] || fail "ADB_ICMP_DEVICE is required unless --no-icmp is used"
fi
[[ "$DURATION" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "duration_s must be numeric"
[[ "$RATE" =~ ^[0-9]+([.][0-9]+)?([KMG]([bB][pP][sS])?|pps)?$ ]] || fail "invalid rate"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || fail "invalid port"
[[ "$PAYLOAD_BYTES" =~ ^[0-9]+$ ]] && (( PAYLOAD_BYTES >= 48 )) || fail "payload_bytes must be at least 48"
[[ "$UDP_RECV_BUFFER_BYTES" =~ ^[0-9]+$ && "$UDP_RECV_BUFFER_BYTES" -ge 65536 ]] || fail "UDP_RECV_BUFFER_BYTES must be at least 65536"
[[ "$PING_INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "ping_interval_s must be numeric"
[[ "$PROGRESS_INTERVAL_S" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "UDP_PROGRESS_INTERVAL_S must be numeric"
awk -v interval="$PROGRESS_INTERVAL_S" 'BEGIN { exit !(interval > 0) }' || fail "UDP_PROGRESS_INTERVAL_S must be positive"
[[ "$MONITOR_INTERVAL_S" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "UDP_MONITOR_INTERVAL_S must be numeric"
awk -v interval="$MONITOR_INTERVAL_S" 'BEGIN { exit !(interval > 0) }' || fail "UDP_MONITOR_INTERVAL_S must be positive"
[[ "$GAP_MS" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "UDP_GAP_MS must be numeric"
[[ "$POSTPROCESS" == "arrival" || "$POSTPROCESS" == "latency" ]] || fail "UDP_POSTPROCESS must be arrival or latency"
[[ "$WINDOW_MS" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "UDP_WINDOW_MS must be numeric"
awk -v window="$WINDOW_MS" 'BEGIN { exit !(window > 0) }' || fail "UDP_WINDOW_MS must be positive"
[[ "$UDP_RECEIVE_API" == "auto" || "$UDP_RECEIVE_API" == "readmsg" || "$UDP_RECEIVE_API" == "recvmmsg" ]] || fail "UDP_RECEIVE_API must be auto, readmsg, or recvmmsg"
[[ "$UDP_RECEIVE_BATCH_SIZE" =~ ^[1-9][0-9]*$ && "$UDP_RECEIVE_BATCH_SIZE" -le 256 ]] || fail "UDP_RECEIVE_BATCH_SIZE must be 1..256"
[[ "$UDP_MAX_RECEIVER_SOCKET_DROPS" =~ ^[0-9]+$ ]] || fail "UDP_MAX_RECEIVER_SOCKET_DROPS must be non-negative"
if [[ -n "$UDP_REQUIRE_RECEIVE_API" ]]; then
  [[ "$UDP_REQUIRE_RECEIVE_API" == "readmsg" || "$UDP_REQUIRE_RECEIVE_API" == "recvmmsg" ]] || fail "UDP_REQUIRE_RECEIVE_API must be readmsg or recvmmsg"
fi
[[ "$MIN_RECV_SPAN_MS" =~ ^[0-9]+$ ]] || fail "MIN_RECV_SPAN_MS must be a non-negative integer"
[[ "$MAX_RECV_SPAN_MS" =~ ^[0-9]+$ ]] || fail "MAX_RECV_SPAN_MS must be a non-negative integer"
(( MIN_RECV_SPAN_MS <= MAX_RECV_SPAN_MS )) || fail "MIN_RECV_SPAN_MS must not exceed MAX_RECV_SPAN_MS"
if (( ICMP_ENABLED )); then
  [[ "$UDP_DEVICE" != "$ICMP_DEVICE" ]] || fail "UDP and ICMP devices must differ"
fi
[[ "$MIN_ARRIVAL_MAX_MS" =~ ^[0-9]+$ ]] || fail "MIN_ARRIVAL_MAX_MS must be a non-negative integer"
[[ "$MAX_ARRIVAL_MAX_MS" =~ ^[0-9]+$ ]] || fail "MAX_ARRIVAL_MAX_MS must be a non-negative integer"
(( MIN_ARRIVAL_MAX_MS <= MAX_ARRIVAL_MAX_MS )) || fail "MIN_ARRIVAL_MAX_MS must not exceed MAX_ARRIVAL_MAX_MS"
[[ "$DURATION_MODE" == "packet-count" || "$DURATION_MODE" == "wall-clock" ]] || fail "UDP_DURATION_MODE must be packet-count or wall-clock"
awk -v min="$MIN_SENDER_ELAPSED_S" -v max="$MAX_SENDER_ELAPSED_S" 'BEGIN { exit !(min >= 0 && min <= max) }' || fail "invalid sender elapsed bounds"

if [[ -z "$SERVER" ]]; then
  SERVER="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" "ip -4 route get 8.8.8.8 | awk '{for (i=1; i<=NF; i++) if (\$i==\"src\") {print \$(i+1); exit}}'" 2>/dev/null | tr -d '\r' | awk 'NF {print; exit}')"
fi
[[ -n "$SERVER" ]] || fail "server IP is required or could not be resolved"

for command in adb ssh scp tar go python3; do command -v "$command" >/dev/null || fail "$command is required"; done
adb devices > /tmp/custom_udp_uplink_adb_devices.$$ 2>&1
for device in "$UDP_DEVICE"; do
  awk -v d="$device" '$1 == d && $2 == "device" { found=1 } END { exit !found }' /tmp/custom_udp_uplink_adb_devices.$$ || fail "ADB device is not ready: $device"
done
UDP_ABI_LIST="$(adb -s "$UDP_DEVICE" shell getprop ro.product.cpu.abilist 2>/dev/null | tr -d '\r' || true)"
UDP_BOOT_COMPLETED="$(adb -s "$UDP_DEVICE" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
UDP_MODEL="$(adb -s "$UDP_DEVICE" shell getprop ro.product.model 2>/dev/null | tr -d '\r' || true)"
WIFI_LINE="$(adb -s "$UDP_DEVICE" shell dumpsys wifi 2>/dev/null | awk '/mWifiInfo SSID:/ {print; exit}' | tr -d '\r' || true)"
WIFI_SSID="$(printf '%s\n' "$WIFI_LINE" | awk -F'SSID: ' 'NF > 1 {split($2, parts, ","); gsub(/[\"]/, "", parts[1]); print parts[1]; exit}')"
WIFI_BSSID="$(printf '%s\n' "$WIFI_LINE" | sed -n 's/.*BSSID: \([^, ]*\).*/\1/p')"
WIFI_RSSI="$(printf '%s\n' "$WIFI_LINE" | sed -n 's/.*RSSI: \(-*[0-9][0-9]*\).*/\1/p')"
WIFI_LINK_SPEED="$(printf '%s\n' "$WIFI_LINE" | sed -n 's/.*Link speed: \([0-9][0-9]*\)Mbps.*/\1/p')"
[[ "$UDP_ABI_LIST" == *arm* && "$UDP_BOOT_COMPLETED" == "1" ]] || fail "UDP device shell is not ready: $UDP_DEVICE"
[[ -z "$EXPECTED_WIFI_SSID" || "$WIFI_SSID" == "$EXPECTED_WIFI_SSID" ]] || fail "unexpected Wi-Fi SSID: expected=$EXPECTED_WIFI_SSID actual=$WIFI_SSID"
[[ -z "$EXPECTED_DEVICE_MODEL" || "$UDP_MODEL" == "$EXPECTED_DEVICE_MODEL" ]] || fail "unexpected device model: expected=$EXPECTED_DEVICE_MODEL actual=$UDP_MODEL"
if (( ICMP_ENABLED )); then
  awk -v d="$ICMP_DEVICE" '$1 == d && $2 == "device" { found=1 } END { exit !found }' /tmp/custom_udp_uplink_adb_devices.$$ || fail "ADB device is not ready: $ICMP_DEVICE"
  ICMP_BOOT_COMPLETED="$(adb -s "$ICMP_DEVICE" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
  [[ "$ICMP_BOOT_COMPLETED" == "1" ]] || fail "ICMP device shell is not ready: $ICMP_DEVICE"
fi
rm -f /tmp/custom_udp_uplink_adb_devices.$$
SERVER_PLATFORM="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" 'uname -s -m' 2>/dev/null | tr -d '\r')"
[[ "$SERVER_PLATFORM" == "Linux x86_64" ]] || fail "uplink Go receiver requires Linux x86_64 server, got: $SERVER_PLATFORM"
SERVER_PORT_LISTENER="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "ss -H -lun | awk '\$5 ~ /:${PORT}\$/ {print}'" 2>/dev/null || true)"
[[ -z "$SERVER_PORT_LISTENER" ]] || fail "UDP port $PORT is already in use on $SERVER_SSH"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
SAFE_RATE="$(printf '%s' "$RATE" | tr -c 'A-Za-z0-9._-' '_')"
RUN_ID="dual_udp_latency_uplink_${TS}_${SAFE_RATE}_${DURATION}s"
RUN_DIR="$LOG_ROOT/$RUN_ID"
UDP_DIR="$RUN_DIR/udp_phone"
ICMP_DIR="$RUN_DIR/icmp_phone"
SERVER_DIR="$RUN_DIR/server"
REMOTE_ROOT="${REMOTE_ROOT:-/data/local/tmp/starlink_measure}"
PHONE_REMOTE_ROOT="$REMOTE_ROOT/$RUN_ID"
PHONE_UDP_DIR="$PHONE_REMOTE_ROOT/udp"
PHONE_ICMP_DIR="$PHONE_REMOTE_ROOT/icmp"
SERVER_REMOTE_DIR="$SERVER_REPO/traces/logs/$RUN_ID/server"
if (( ICMP_ENABLED )); then
  PING_COUNT="$(awk -v d="$DURATION" -v i="$PING_INTERVAL" 'BEGIN { printf "%d", int(d / i + 0.999999) }')"
else
  PING_COUNT=0
fi

cleanup_failed_server() {
  local status="$?"
  if [[ -n "${MONITOR_PID:-}" ]]; then
    kill "$MONITOR_PID" >/dev/null 2>&1 || true
    wait "$MONITOR_PID" >/dev/null 2>&1 || true
  fi
  if [[ "$status" -ne 0 && -n "${RUN_ID:-}" ]]; then
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" \
      "for pid in \$(ps -eo pid=,args= | awk -v run_id='$RUN_ID' '\$0 ~ /uplink_udp_receiver/ && index(\$0, run_id) {print \$1}'); do kill -TERM \"\$pid\" 2>/dev/null || true; done" \
      >/dev/null 2>&1 || true
  fi
  return "$status"
}
trap cleanup_failed_server EXIT

mkdir -p "$UDP_DIR/remote" "$SERVER_DIR/remote"
if (( ICMP_ENABLED )); then
  mkdir -p "$ICMP_DIR/remote"
fi
cat > "$RUN_DIR/metadata.txt" <<META
run_id=$RUN_ID
status=starting
type=dual_udp_latency_uplink
server=$SERVER
server_ssh=$SERVER_SSH
server_repo=$SERVER_REPO
udp_direction=uplink
duration_s=$DURATION
udp_rate=$RATE
udp_payload_len=$PAYLOAD_BYTES
udp_port=$PORT
icmp_enabled=$([[ "$ICMP_ENABLED" -eq 1 ]] && echo true || echo false)
ping_interval_s=$([[ "$ICMP_ENABLED" -eq 1 ]] && echo "$PING_INTERVAL" || echo disabled)
ping_count=$PING_COUNT
postprocess=$POSTPROCESS
window_ms=$WINDOW_MS
duration_mode=$DURATION_MODE
progress_interval_s=$PROGRESS_INTERVAL_S
monitor_interval_s=$MONITOR_INTERVAL_S
min_recv_span_ms=$MIN_RECV_SPAN_MS
max_recv_span_ms=$MAX_RECV_SPAN_MS
udp_device=$UDP_DEVICE
expected_wifi_ssid=$EXPECTED_WIFI_SSID
expected_device_model=$EXPECTED_DEVICE_MODEL
device_model=$UDP_MODEL
wifi_ssid=$WIFI_SSID
wifi_bssid=$WIFI_BSSID
wifi_rssi=$WIFI_RSSI
wifi_link_speed_mbps=$WIFI_LINK_SPEED
server_platform=$SERVER_PLATFORM
server_port_listener=${SERVER_PORT_LISTENER:-none}
icmp_device=$([[ "$ICMP_ENABLED" -eq 1 ]] && echo "$ICMP_DEVICE" || echo disabled)
protocol=UWLP_v2
clock_timestamp_fields=phone_send_unix_ns,server_recv_unix_ns
receive_api_requested=$UDP_RECEIVE_API
receive_batch_size=$UDP_RECEIVE_BATCH_SIZE
max_receiver_socket_drops=$UDP_MAX_RECEIVER_SOCKET_DROPS
require_receive_api=$UDP_REQUIRE_RECEIVE_API
receiver_impl=go_linux_amd64
note=$([[ "$ICMP_ENABLED" -eq 1 ]] && echo 'custom timestamped UDP uplink plus independent ICMP' || echo 'custom timestamped UDP uplink without ICMP')
META

if [[ ",$UDP_ABI_LIST," == *,arm64-v8a,* ]]; then
  SENDER_GOOS=android; SENDER_GOARCH=arm64; SENDER_TAG=arm64
elif [[ ",$UDP_ABI_LIST," == *,armeabi-v7a,* ]]; then
  SENDER_GOOS=linux; SENDER_GOARCH=arm; SENDER_TAG=armv7
else
  fail "unsupported UDP phone ABI: $UDP_ABI_LIST"
fi
SENDER_BIN="$RUN_DIR/uplink_udp_sender_${SENDER_TAG}"
if [[ "$SENDER_TAG" == "armv7" ]]; then
  GOOS="$SENDER_GOOS" GOARCH="$SENDER_GOARCH" GOARM=7 CGO_ENABLED=0 go build -trimpath -ldflags '-s -w' -o "$SENDER_BIN" "$REPO_DIR/mac_reemo/uplink_udp_sender.go" > "$UDP_DIR/go_build.stdout" 2> "$UDP_DIR/go_build.stderr"
else
  GOOS="$SENDER_GOOS" GOARCH="$SENDER_GOARCH" CGO_ENABLED=0 go build -trimpath -ldflags '-s -w' -o "$SENDER_BIN" "$REPO_DIR/mac_reemo/uplink_udp_sender.go" > "$UDP_DIR/go_build.stdout" 2> "$UDP_DIR/go_build.stderr"
fi
RECEIVER_BIN="$RUN_DIR/uplink_udp_receiver_linux_amd64"
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags '-s -w' -o "$RECEIVER_BIN" "$REPO_DIR/server_receiver/scripts/uplink_udp_receiver.go" > "$SERVER_DIR/go_build.stdout" 2> "$SERVER_DIR/go_build.stderr" || {
  cat "$SERVER_DIR/go_build.stderr" >&2
  fail "failed to build Linux uplink receiver"
}
adb -s "$UDP_DEVICE" push "$SENDER_BIN" /data/local/tmp/uplink_udp_sender > "$UDP_DIR/adb_push_sender.stdout" 2> "$UDP_DIR/adb_push_sender.stderr"
adb -s "$UDP_DEVICE" shell chmod 755 /data/local/tmp/uplink_udp_sender
adb -s "$UDP_DEVICE" shell /data/local/tmp/uplink_udp_sender --version > "$UDP_DIR/sender_version.stdout" 2> "$UDP_DIR/sender_version.stderr"

if [[ "$POSTPROCESS" == "arrival" ]]; then
  cat > "$RUN_DIR/server_runner.sh" <<'SERVER_RUNNER'
#!/usr/bin/env bash
set -euo pipefail
REMOTE_DIR="$1"; PORT="$2"; DURATION="$3"; TIMEOUT="$4"; RUN_ID="$5"; PROGRESS_INTERVAL="$6"; WINDOW_MS="$7"; GAP_MS="$8"; RECV_BUFFER="$9"; PAYLOAD="${10}"; RECEIVE_API="${11}"; BATCH_SIZE="${12}"
mkdir -p "$REMOTE_DIR"
"$REMOTE_DIR/uplink_udp_receiver" --port "$PORT" --duration-s "$DURATION" --timeout-s "$TIMEOUT" --run-id "$RUN_ID" --out-dir "$REMOTE_DIR" --output-mode compact --postprocess arrival --payload-bytes "$PAYLOAD" --recv-buffer-bytes "$RECV_BUFFER" --receive-api "$RECEIVE_API" --receive-batch-size "$BATCH_SIZE" --ready-file "$REMOTE_DIR/ready" --progress-path "$REMOTE_DIR/uplink_udp_receiver_progress.tsv" --progress-interval "${PROGRESS_INTERVAL}s"
python3 "$REMOTE_DIR/CompactUDPTrace.py" "$REMOTE_DIR/udp_trace.utrc" --output-dir "$REMOTE_DIR" --mode arrival --gap-ms "$GAP_MS"
gzip -9 -c "$REMOTE_DIR/udp_arrival_time_ms.txt" > "$REMOTE_DIR/udp_arrival_time_ms.txt.gz"
rm -f "$REMOTE_DIR/udp_trace.utrc"
SERVER_RUNNER
else
  cat > "$RUN_DIR/server_runner.sh" <<'SERVER_RUNNER'
#!/usr/bin/env bash
set -euo pipefail
REMOTE_DIR="$1"; PORT="$2"; DURATION="$3"; TIMEOUT="$4"; RUN_ID="$5"; PROGRESS_INTERVAL="$6"; WINDOW_MS="$7"; GAP_MS="$8"; RECV_BUFFER="$9"; PAYLOAD="${10}"; RECEIVE_API="${11}"; BATCH_SIZE="${12}"
mkdir -p "$REMOTE_DIR"
"$REMOTE_DIR/uplink_udp_receiver" --port "$PORT" --duration-s "$DURATION" --timeout-s "$TIMEOUT" --run-id "$RUN_ID" --out-dir "$REMOTE_DIR" --output-mode compact --postprocess latency --payload-bytes "$PAYLOAD" --recv-buffer-bytes "$RECV_BUFFER" --receive-api "$RECEIVE_API" --receive-batch-size "$BATCH_SIZE" --ready-file "$REMOTE_DIR/ready" --progress-path "$REMOTE_DIR/uplink_udp_receiver_progress.tsv" --progress-interval "${PROGRESS_INTERVAL}s"
python3 "$REMOTE_DIR/CompactUDPTrace.py" "$REMOTE_DIR/udp_trace.utrc" --output-dir "$REMOTE_DIR" --mode latency --window-ms "$WINDOW_MS" --gap-ms "$GAP_MS"
gzip -9 -c "$REMOTE_DIR/delay.txt" > "$REMOTE_DIR/delay.txt.gz"
rm -f "$REMOTE_DIR/udp_trace.utrc"
SERVER_RUNNER
fi
cat > "$RUN_DIR/udp_runner.sh" <<'UDP_RUNNER'
#!/system/bin/sh
set -u
DIR="$1"; BIN="$2"; RUN_ID="$3"; SERVER="$4"; PORT="$5"; DURATION="$6"; RATE="$7"; PAYLOAD="$8"; NO_CSV="$9"; DURATION_MODE="${10}"; PROGRESS_INTERVAL="${11}"
mkdir -p "$DIR"
if [ "$NO_CSV" = "1" ]; then
  "$BIN" --run-id "$RUN_ID" --target "$SERVER" --port "$PORT" --duration-s "$DURATION" --rate "$RATE" --payload-bytes "$PAYLOAD" --duration-mode "$DURATION_MODE" --progress-interval "${PROGRESS_INTERVAL}s" --out-dir "$DIR" --no-csv
else
  "$BIN" --run-id "$RUN_ID" --target "$SERVER" --port "$PORT" --duration-s "$DURATION" --rate "$RATE" --payload-bytes "$PAYLOAD" --duration-mode "$DURATION_MODE" --progress-interval "${PROGRESS_INTERVAL}s" --out-dir "$DIR"
fi
UDP_RUNNER
if (( ICMP_ENABLED )); then
cat > "$RUN_DIR/icmp_runner.sh" <<'ICMP_RUNNER'
#!/system/bin/sh
set -u
DIR="$1"; SERVER="$2"; INTERVAL="$3"; COUNT="$4"
mkdir -p "$DIR"
ping -D -i "$INTERVAL" -c "$COUNT" "$SERVER" > "$DIR/ping.log" 2> "$DIR/ping.stderr"
ICMP_RUNNER
fi
chmod +x "$RUN_DIR/server_runner.sh" "$RUN_DIR/udp_runner.sh"
if (( ICMP_ENABLED )); then
  chmod +x "$RUN_DIR/icmp_runner.sh"
fi

ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" "mkdir -p '$SERVER_REMOTE_DIR'"
scp_retry 30 "$REPO_DIR/server_receiver/scripts/uplink_udp_receiver.py" "$SERVER_SSH:$SERVER_REMOTE_DIR/uplink_udp_receiver.py" > "$SERVER_DIR/scp_source.stdout" 2> "$SERVER_DIR/scp_source.stderr"
scp_retry 30 "$RECEIVER_BIN" "$SERVER_SSH:$SERVER_REMOTE_DIR/uplink_udp_receiver" >> "$SERVER_DIR/scp_source.stdout" 2>> "$SERVER_DIR/scp_source.stderr"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" "chmod 755 '$SERVER_REMOTE_DIR/uplink_udp_receiver'"
scp_retry 30 "$TRACE_SCRIPT_DIR/UDPProgressAudit.py" "$SERVER_SSH:$SERVER_REMOTE_DIR/UDPProgressAudit.py" >> "$SERVER_DIR/scp_source.stdout" 2>> "$SERVER_DIR/scp_source.stderr"
scp_retry 30 "$TRACE_SCRIPT_DIR/CompactUDPTrace.py" "$SERVER_SSH:$SERVER_REMOTE_DIR/CompactUDPTrace.py" >> "$SERVER_DIR/scp_source.stdout" 2>> "$SERVER_DIR/scp_source.stderr"
if [[ "$POSTPROCESS" == "arrival" ]]; then
  scp_retry 30 "$TRACE_SCRIPT_DIR/UDPArrivalTimeProcess.py" "$SERVER_SSH:$SERVER_REMOTE_DIR/UDPArrivalTimeProcess.py" >> "$SERVER_DIR/scp_source.stdout" 2>> "$SERVER_DIR/scp_source.stderr"
else
  scp_retry 30 "$TRACE_SCRIPT_DIR/UDPLatencyWindowProcess.py" "$SERVER_SSH:$SERVER_REMOTE_DIR/UDPLatencyWindowProcess.py" >> "$SERVER_DIR/scp_source.stdout" 2>> "$SERVER_DIR/scp_source.stderr"
fi
ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" "bash -s -- '$SERVER_REMOTE_DIR' '$PORT' '$DURATION' '$UDP_TIMEOUT_S' '$RUN_ID' '$PROGRESS_INTERVAL_S' '$WINDOW_MS' '$GAP_MS' '$UDP_RECV_BUFFER_BYTES' '$PAYLOAD_BYTES' '$UDP_RECEIVE_API' '$UDP_RECEIVE_BATCH_SIZE'" < "$RUN_DIR/server_runner.sh" > "$SERVER_DIR/ssh.stdout" 2> "$SERVER_DIR/ssh.stderr" &
SERVER_PID=$!
for _ in $(seq 1 30); do
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "test -f '$SERVER_REMOTE_DIR/ready'" && break
  sleep 1
done
ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "test -f '$SERVER_REMOTE_DIR/ready'" || fail "server receiver did not become ready"

adb -s "$UDP_DEVICE" shell "mkdir -p '$PHONE_UDP_DIR'"
adb -s "$UDP_DEVICE" push "$RUN_DIR/udp_runner.sh" "$PHONE_UDP_DIR/runner.sh" > "$UDP_DIR/adb_push_runner.stdout" 2> "$UDP_DIR/adb_push_runner.stderr"
adb -s "$UDP_DEVICE" shell "chmod 755 '$PHONE_UDP_DIR/runner.sh'"
if (( ICMP_ENABLED )); then
  adb -s "$ICMP_DEVICE" shell "mkdir -p '$PHONE_ICMP_DIR'"
  adb -s "$ICMP_DEVICE" push "$RUN_DIR/icmp_runner.sh" "$PHONE_ICMP_DIR/runner.sh" > "$ICMP_DIR/adb_push_runner.stdout" 2> "$ICMP_DIR/adb_push_runner.stderr"
  adb -s "$ICMP_DEVICE" shell "chmod 755 '$PHONE_ICMP_DIR/runner.sh'"
fi

MONITOR_DURATION_S="$(awk -v duration="$DURATION" -v timeout="$UDP_TIMEOUT_S" 'BEGIN { printf "%.3f", duration + timeout + 3 }')"
python3 "$TRACE_SCRIPT_DIR/MonitorUDPProgress.py" \
  --adb-device "$UDP_DEVICE" \
  --phone-path "$PHONE_UDP_DIR/uplink_udp_sender_progress.tsv" \
  --server-ssh "$SERVER_SSH" \
  --server-path "$SERVER_REMOTE_DIR/uplink_udp_receiver_progress.tsv" \
  --output "$RUN_DIR/udp_progress_monitor.tsv" \
  --interval-s "$MONITOR_INTERVAL_S" \
  --duration-s "$MONITOR_DURATION_S" \
  --command-timeout-s 10 \
  > "$RUN_DIR/udp_progress_monitor.stdout" 2> "$RUN_DIR/udp_progress_monitor.stderr" &
MONITOR_PID=$!

if (( ICMP_ENABLED )); then
  adb -s "$ICMP_DEVICE" shell "sh '$PHONE_ICMP_DIR/runner.sh' '$PHONE_ICMP_DIR' '$SERVER' '$PING_INTERVAL' '$PING_COUNT'" > "$ICMP_DIR/adb_runner.stdout" 2> "$ICMP_DIR/adb_runner.stderr" &
  ICMP_PID=$!
else
  ICMP_PID=""
  ICMP_RC=0
  ICMP_PULL_RC=0
fi
UDP_NO_CSV=1
adb -s "$UDP_DEVICE" shell "sh '$PHONE_UDP_DIR/runner.sh' '$PHONE_UDP_DIR' '/data/local/tmp/uplink_udp_sender' '$RUN_ID' '$SERVER' '$PORT' '$DURATION' '$RATE' '$PAYLOAD_BYTES' '$UDP_NO_CSV' '$DURATION_MODE' '$PROGRESS_INTERVAL_S'" > "$UDP_DIR/adb_runner.stdout" 2> "$UDP_DIR/adb_runner.stderr" &
UDP_PID=$!

set +e
if (( ICMP_ENABLED )); then
  wait "$ICMP_PID"; ICMP_RC=$?
fi
wait "$UDP_PID"; UDP_RC=$?
wait "$SERVER_PID"; SERVER_RC=$?
if [[ -n "${MONITOR_PID:-}" ]]; then
  wait "$MONITOR_PID" >/dev/null 2>&1
  MONITOR_RC=$?
else
  MONITOR_RC=1
fi
[[ -s "$RUN_DIR/udp_progress_monitor.tsv" ]] || MONITOR_RC=1
set -e
if (( ICMP_ENABLED )); then
  printf '%s\n' "$ICMP_RC" > "$ICMP_DIR/adb_exit_code.txt"
fi
printf '%s\n' "$UDP_RC" > "$UDP_DIR/adb_exit_code.txt"
printf '%s\n' "$SERVER_RC" > "$SERVER_DIR/ssh_exit_code.txt"

set +e
mkdir -p "$UDP_DIR/remote"
run_with_timeout 30 adb -s "$UDP_DEVICE" pull "$PHONE_UDP_DIR/uplink_udp_sender_summary.txt" "$UDP_DIR/remote/" > "$UDP_DIR/adb_pull.stdout" 2> "$UDP_DIR/adb_pull.stderr"; UDP_PULL_RC=$?
run_with_timeout 30 adb -s "$UDP_DEVICE" pull "$PHONE_UDP_DIR/uplink_udp_sender_progress.tsv" "$UDP_DIR/remote/" >> "$UDP_DIR/adb_pull.stdout" 2>> "$UDP_DIR/adb_pull.stderr"; UDP_PROGRESS_PULL_RC=$?
ICMP_TRANSMITTED_PACKETS=""
ICMP_RECEIVED_PACKETS=""
ICMP_PACKET_LOSS_PERCENT=""
if (( ICMP_ENABLED )); then
  run_with_timeout 120 adb -s "$ICMP_DEVICE" pull "$PHONE_ICMP_DIR/." "$ICMP_DIR/remote" > "$ICMP_DIR/adb_pull.stdout" 2> "$ICMP_DIR/adb_pull.stderr"; ICMP_PULL_RC=$?
  if [[ "$ICMP_PULL_RC" -eq 0 && -s "$ICMP_DIR/remote/ping.log" ]]; then
    python3 "$TRACE_SCRIPT_DIR/ICMPPingSummaryProcess.py" \
      "$ICMP_DIR/remote/ping.log" \
      --expected-transmitted "$PING_COUNT" \
      --output "$ICMP_DIR/remote/ping_summary.txt" \
      > "$ICMP_DIR/ping_summary.stdout" 2> "$ICMP_DIR/ping_summary.stderr" || ICMP_PULL_RC=1
  else
    ICMP_PULL_RC=1
  fi
  if [[ "$ICMP_PULL_RC" -eq 0 && -s "$ICMP_DIR/remote/ping_summary.txt" ]]; then
    ICMP_TRANSMITTED_PACKETS="$(awk -F= '$1 == "icmp_transmitted_packets" {print $2; exit}' "$ICMP_DIR/remote/ping_summary.txt")"
    ICMP_RECEIVED_PACKETS="$(awk -F= '$1 == "icmp_received_packets" {print $2; exit}' "$ICMP_DIR/remote/ping_summary.txt")"
    ICMP_PACKET_LOSS_PERCENT="$(awk -F= '$1 == "icmp_packet_loss_percent" {print $2; exit}' "$ICMP_DIR/remote/ping_summary.txt")"
    [[ "$ICMP_TRANSMITTED_PACKETS" =~ ^[0-9]+$ && "$ICMP_TRANSMITTED_PACKETS" -eq "$PING_COUNT" ]] || ICMP_PULL_RC=1
    [[ "$ICMP_RECEIVED_PACKETS" =~ ^[1-9][0-9]*$ ]] || ICMP_PULL_RC=1
    [[ "$ICMP_PACKET_LOSS_PERCENT" =~ ^[0-9]+([.][0-9]+)?$ ]] || ICMP_PULL_RC=1
  fi
else
  ICMP_PULL_RC=0
fi
[[ -f "$UDP_DIR/remote/uplink_udp_sender_summary.txt" ]] && cp "$UDP_DIR/remote/uplink_udp_sender_summary.txt" "$UDP_DIR/uplink_udp_sender_summary.txt"
SERVER_PULL_RC=0
if [[ "$POSTPROCESS" == "arrival" ]]; then
  scp_retry 60 "$SERVER_SSH:$SERVER_REMOTE_DIR/udp_arrival_time_ms.txt.gz" "$SERVER_DIR/remote/" > "$SERVER_DIR/scp.stdout" 2> "$SERVER_DIR/scp.stderr" || SERVER_PULL_RC=1
  scp_retry 30 "$SERVER_SSH:$SERVER_REMOTE_DIR/uplink_udp_receiver_summary.txt" "$SERVER_DIR/remote/" >> "$SERVER_DIR/scp.stdout" 2>> "$SERVER_DIR/scp.stderr" || SERVER_PULL_RC=1
else
  scp_retry 60 "$SERVER_SSH:$SERVER_REMOTE_DIR/delay.txt" "$SERVER_DIR/remote/" > "$SERVER_DIR/scp.stdout" 2> "$SERVER_DIR/scp.stderr" || SERVER_PULL_RC=1
  scp_retry 30 "$SERVER_SSH:$SERVER_REMOTE_DIR/delay_window_time_ms.txt" "$SERVER_DIR/remote/" >> "$SERVER_DIR/scp.stdout" 2>> "$SERVER_DIR/scp.stderr" || SERVER_PULL_RC=1
  scp_retry 30 "$SERVER_SSH:$SERVER_REMOTE_DIR/delay_summary.txt" "$SERVER_DIR/remote/" >> "$SERVER_DIR/scp.stdout" 2>> "$SERVER_DIR/scp.stderr" || SERVER_PULL_RC=1
  scp_retry 30 "$SERVER_SSH:$SERVER_REMOTE_DIR/uplink_udp_receiver_summary.txt" "$SERVER_DIR/remote/" >> "$SERVER_DIR/scp.stdout" 2>> "$SERVER_DIR/scp.stderr" || SERVER_PULL_RC=1
  if [[ -s "$SERVER_DIR/remote/delay.txt" ]]; then gzip -9 -c "$SERVER_DIR/remote/delay.txt" > "$SERVER_DIR/remote/delay.txt.gz"; fi
fi
scp_retry 30 "$SERVER_SSH:$SERVER_REMOTE_DIR/uplink_udp_receiver_progress.tsv" "$SERVER_DIR/remote/" >> "$SERVER_DIR/scp.stdout" 2>> "$SERVER_DIR/scp.stderr" || SERVER_PULL_RC=1
scp_retry 30 "$SERVER_SSH:$SERVER_REMOTE_DIR/udp_receive_gap_report.tsv" "$SERVER_DIR/remote/" >> "$SERVER_DIR/scp.stdout" 2>> "$SERVER_DIR/scp.stderr" || SERVER_PULL_RC=1
set -e
if [[ "$POSTPROCESS" == "arrival" && -s "$SERVER_DIR/remote/udp_arrival_time_ms.txt.gz" ]]; then
  if gzip -t "$SERVER_DIR/remote/udp_arrival_time_ms.txt.gz" && \
     gzip -dc "$SERVER_DIR/remote/udp_arrival_time_ms.txt.gz" > "$SERVER_DIR/.udp_arrival_time_ms.txt.tmp"; then
    mv "$SERVER_DIR/.udp_arrival_time_ms.txt.tmp" "$SERVER_DIR/udp_arrival_time_ms.txt"
    cp "$SERVER_DIR/remote/udp_arrival_time_ms.txt.gz" "$SERVER_DIR/udp_arrival_time_ms.txt.gz"
  else
    rm -f "$SERVER_DIR/.udp_arrival_time_ms.txt.tmp"
    SERVER_PULL_RC=1
  fi
fi
if [[ "$POSTPROCESS" == "latency" && -s "$SERVER_DIR/remote/delay.txt.gz" ]]; then
  if gzip -t "$SERVER_DIR/remote/delay.txt.gz" && \
     gzip -dc "$SERVER_DIR/remote/delay.txt.gz" > "$SERVER_DIR/.delay.txt.tmp"; then
    mv "$SERVER_DIR/.delay.txt.tmp" "$SERVER_DIR/delay.txt"
    cp "$SERVER_DIR/remote/delay.txt.gz" "$SERVER_DIR/delay.txt.gz"
  else
    rm -f "$SERVER_DIR/.delay.txt.tmp"
    SERVER_PULL_RC=1
  fi
fi
for name in uplink_udp_receiver_summary.txt delay_summary.txt uplink_udp_receiver_progress.tsv udp_receive_gap_report.tsv; do
  [[ -f "$SERVER_DIR/remote/$name" ]] && cp "$SERVER_DIR/remote/$name" "$SERVER_DIR/$name"
done
if [[ "$POSTPROCESS" == "arrival" && -s "$SERVER_DIR/udp_arrival_time_ms.txt" ]]; then
  cp "$SERVER_DIR/udp_arrival_time_ms.txt" "$RUN_DIR/udp_arrival_time_ms.txt"
  [[ -s "$SERVER_DIR/udp_arrival_time_ms.txt.gz" ]] && cp "$SERVER_DIR/udp_arrival_time_ms.txt.gz" "$RUN_DIR/udp_arrival_time_ms.txt.gz"
  PLOT_ARGS=("$RUN_DIR" --window-ms "$WINDOW_MS" --output "$RUN_DIR/bandwidth_time_scatter.png")
  PLOT_DURATION_MS="$(awk -F= '$1 == "duration_s" { printf "%.0f", $2 * 1000; exit }' "$RUN_DIR/metadata.txt")"
  if [[ "$PLOT_DURATION_MS" =~ ^[0-9]+$ ]]; then
    PLOT_ARGS+=(--duration-ms "$PLOT_DURATION_MS")
  fi
  python3 "$TRACE_SCRIPT_DIR/BandwidthTimeScatter.py" "${PLOT_ARGS[@]}" > "$RUN_DIR/bandwidth_plot.stdout" 2> "$RUN_DIR/bandwidth_plot.stderr" || true
fi
if [[ "$POSTPROCESS" == "latency" && -s "$SERVER_DIR/delay.txt" ]]; then
  cp "$SERVER_DIR/delay.txt" "$RUN_DIR/delay.txt"
  cp "$SERVER_DIR/delay.txt.gz" "$RUN_DIR/delay.txt.gz"
  [[ -s "$SERVER_DIR/remote/delay_window_time_ms.txt" ]] && cp "$SERVER_DIR/remote/delay_window_time_ms.txt" "$RUN_DIR/delay_window_time_ms.txt"
  [[ -s "$RUN_DIR/time_delay_scatter.png" ]] || python3 "$TRACE_SCRIPT_DIR/DelayTimeScatter.py" "$RUN_DIR" > "$RUN_DIR/delay_plot.stdout" 2> "$RUN_DIR/delay_plot.stderr" || true
fi
PROGRESS_AUDIT_RC=1
if [[ -s "$SERVER_DIR/udp_receive_gap_report.tsv" &&
      -s "$UDP_DIR/remote/uplink_udp_sender_progress.tsv" &&
      -s "$SERVER_DIR/remote/uplink_udp_receiver_progress.tsv" &&
      -s "$UDP_DIR/uplink_udp_sender_summary.txt" ]]; then
  python3 "$TRACE_SCRIPT_DIR/UDPProgressAudit.py" classify \
    "$SERVER_DIR/udp_receive_gap_report.tsv" \
    --sender-progress "$UDP_DIR/remote/uplink_udp_sender_progress.tsv" \
    --receiver-progress "$SERVER_DIR/remote/uplink_udp_receiver_progress.tsv" \
    --sender-summary "$UDP_DIR/uplink_udp_sender_summary.txt" \
    --output "$RUN_DIR/udp_receive_gap_audit.tsv" \
    > "$RUN_DIR/udp_receive_gap_audit.stdout" 2> "$RUN_DIR/udp_receive_gap_audit.stderr" && PROGRESS_AUDIT_RC=0
fi
if (( ICMP_ENABLED )); then
  [[ -f "$ICMP_DIR/remote/ping.log" ]] || ICMP_PULL_RC=1
fi
[[ -s "$UDP_DIR/uplink_udp_sender_summary.txt" ]] || UDP_PULL_RC=1
[[ -s "$UDP_DIR/remote/uplink_udp_sender_progress.tsv" ]] || UDP_PROGRESS_PULL_RC=1
[[ -s "$SERVER_DIR/remote/uplink_udp_receiver_progress.tsv" ]] || SERVER_PULL_RC=1
[[ -s "$SERVER_DIR/udp_receive_gap_report.tsv" ]] || SERVER_PULL_RC=1
if [[ "$POSTPROCESS" == "arrival" ]]; then
  [[ -s "$SERVER_DIR/udp_arrival_time_ms.txt" ]] || SERVER_PULL_RC=1
  [[ -s "$SERVER_DIR/udp_arrival_time_ms.txt.gz" ]] || SERVER_PULL_RC=1
else
  [[ -s "$SERVER_DIR/delay.txt" ]] || SERVER_PULL_RC=1
  [[ -s "$SERVER_DIR/delay.txt.gz" ]] || SERVER_PULL_RC=1
  [[ -s "$SERVER_DIR/delay_summary.txt" ]] || SERVER_PULL_RC=1
fi

# The sender summary is authoritative for the scheduled stream.  The expected
# count is computed from the offered rate, duration, and payload size.
EXPECTED_PACKETS="$(awk -v rate="$RATE" -v duration="$DURATION" -v payload="$PAYLOAD_BYTES" '
  BEGIN {
    value = tolower(rate)
    suffix = "pps"
    if (value ~ /gbps$/) { suffix = "gbps"; sub(/gbps$/, "", value) }
    else if (value ~ /mbps$/) { suffix = "mbps"; sub(/mbps$/, "", value) }
    else if (value ~ /kbps$/) { suffix = "kbps"; sub(/kbps$/, "", value) }
    else if (value ~ /pps$/) { suffix = "pps"; sub(/pps$/, "", value) }
    else if (value ~ /g$/) { suffix = "g"; sub(/g$/, "", value) }
    else if (value ~ /m$/) { suffix = "m"; sub(/m$/, "", value) }
    else if (value ~ /k$/) { suffix = "k"; sub(/k$/, "", value) }
    number = value + 0
    pps = number
    if (suffix == "gbps" || suffix == "g") pps = number * 1000000000 / (payload * 8)
    else if (suffix == "mbps" || suffix == "m") pps = number * 1000000 / (payload * 8)
    else if (suffix == "kbps" || suffix == "k") pps = number * 1000 / (payload * 8)
    printf "%d", int(duration * pps + 0.999999999)
  }')"
SCHEDULED_PACKETS=""
ATTEMPTED_PACKETS=""
SENT_PACKETS=""
SEND_FAILED_PACKETS=""
CLOCK_SYNC_SAMPLES=""
SENDER_ELAPSED_S=""
ACTUAL_PAYLOAD_MBPS=""
DEADLINE_REACHED=""
DONE_ACK=""
if [[ -s "$UDP_DIR/uplink_udp_sender_summary.txt" ]]; then
  SCHEDULED_PACKETS="$(awk -F= '$1 == "scheduled_packets" {print $2; exit}' "$UDP_DIR/uplink_udp_sender_summary.txt")"
  ATTEMPTED_PACKETS="$(awk -F= '$1 == "attempted_packets" {print $2; exit}' "$UDP_DIR/uplink_udp_sender_summary.txt")"
  SENT_PACKETS="$(awk -F= '$1 == "sent_packets" {print $2; exit}' "$UDP_DIR/uplink_udp_sender_summary.txt")"
  SEND_FAILED_PACKETS="$(awk -F= '$1 == "send_failed_packets" {print $2; exit}' "$UDP_DIR/uplink_udp_sender_summary.txt")"
  CLOCK_SYNC_SAMPLES="$(awk -F= '$1 == "clock_sync_samples" {print $2; exit}' "$UDP_DIR/uplink_udp_sender_summary.txt")"
  SENDER_ELAPSED_S="$(awk -F= '$1 == "sender_elapsed_s" {print $2; exit}' "$UDP_DIR/uplink_udp_sender_summary.txt")"
  ACTUAL_PAYLOAD_MBPS="$(awk -F= '$1 == "actual_payload_mbps" {print $2; exit}' "$UDP_DIR/uplink_udp_sender_summary.txt")"
  DEADLINE_REACHED="$(awk -F= '$1 == "deadline_reached" {print $2; exit}' "$UDP_DIR/uplink_udp_sender_summary.txt")"
  DONE_ACK="$(awk -F= '$1 == "done_ack" {print $2; exit}' "$UDP_DIR/uplink_udp_sender_summary.txt")"
else
  UDP_PULL_RC=1
fi
[[ "$DONE_ACK" == "true" ]] || UDP_PULL_RC=1
if [[ ! "$SCHEDULED_PACKETS" =~ ^[0-9]+$ || "$SCHEDULED_PACKETS" -ne "$EXPECTED_PACKETS" ]]; then
  UDP_PULL_RC=1
fi
if [[ "$DURATION_MODE" == "packet-count" ]]; then
  if [[ ! "$SENT_PACKETS" =~ ^[0-9]+$ || "$SENT_PACKETS" -ne "$EXPECTED_PACKETS" ]]; then
    UDP_PULL_RC=1
  fi
  [[ "$ATTEMPTED_PACKETS" =~ ^[0-9]+$ && "$ATTEMPTED_PACKETS" -eq "$EXPECTED_PACKETS" ]] || UDP_PULL_RC=1
  [[ "$SEND_FAILED_PACKETS" =~ ^[0-9]+$ && "$SEND_FAILED_PACKETS" -eq 0 ]] || UDP_PULL_RC=1
  [[ "$CLOCK_SYNC_SAMPLES" =~ ^[0-9]+$ && "$CLOCK_SYNC_SAMPLES" -eq 5 ]] || UDP_PULL_RC=1
  [[ "$SENDER_ELAPSED_S" =~ ^[0-9]+([.][0-9]+)?$ ]] || UDP_PULL_RC=1
  awk -v elapsed="$SENDER_ELAPSED_S" -v min="$MIN_SENDER_ELAPSED_S" -v max="$MAX_SENDER_ELAPSED_S" 'BEGIN { exit !(elapsed >= min && elapsed <= max) }' || UDP_PULL_RC=1
else
  [[ "$DURATION_MODE" == "wall-clock" ]] || UDP_PULL_RC=1
  [[ "$ATTEMPTED_PACKETS" =~ ^[1-9][0-9]*$ ]] || UDP_PULL_RC=1
  [[ "$SENT_PACKETS" =~ ^[1-9][0-9]*$ ]] || UDP_PULL_RC=1
  [[ "$DEADLINE_REACHED" == "true" ]] || UDP_PULL_RC=1
  [[ "$SENDER_ELAPSED_S" =~ ^[0-9]+([.][0-9]+)?$ ]] || UDP_PULL_RC=1
  awk -v elapsed="$SENDER_ELAPSED_S" -v min="$MIN_SENDER_ELAPSED_S" -v max="$MAX_SENDER_ELAPSED_S" 'BEGIN { exit !(elapsed >= min && elapsed <= max) }' || UDP_PULL_RC=1
fi

ARRIVAL_ROWS=0
ARRIVAL_MAX_MS=""
PAYLOAD_BYTES_MIN=""
PAYLOAD_BYTES_MAX=""
FIRST_RECV_REL_S=""
LAST_RECV_REL_S=""
AVERAGE_BANDWIDTH_MBPS=""
if [[ -s "$SERVER_DIR/udp_arrival_time_ms.txt" ]]; then
  ARRIVAL_ROWS="$(wc -l < "$SERVER_DIR/udp_arrival_time_ms.txt" | tr -d ' ')"
  ARRIVAL_MAX_MS="$(tail -n 1 "$SERVER_DIR/udp_arrival_time_ms.txt" | tr -d '\r')"
  if ! awk '$0 !~ /^[0-9]+$/ || (NR > 1 && $1 < previous) { bad=1 } { previous=$1 } END { exit bad }' "$SERVER_DIR/udp_arrival_time_ms.txt"; then
    SERVER_PULL_RC=1
  fi
fi
RECEIVED_PACKETS=""
RECEIVER_INTEGRITY=""
SOCKET_RXQ_DROPS=""
RECEIVE_API_EFFECTIVE=""
RECEIVE_CAPTURE_BYTES=""
HEADER_ONLY_PACKETS=""
MISSING_KERNEL_TIMESTAMPS=""
CONTROL_TRUNCATED_PACKETS=""
CAPTURE_BACKPRESSURE_RECORDS=""
DONE_RECEIVED=""
if [[ -s "$SERVER_DIR/uplink_udp_receiver_summary.txt" ]]; then
  RECEIVED_PACKETS="$(awk -F= '$1 == "received_packets" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  PAYLOAD_BYTES_MIN="$(awk -F= '$1 == "payload_bytes_min" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  PAYLOAD_BYTES_MAX="$(awk -F= '$1 == "payload_bytes_max" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  FIRST_RECV_REL_S="$(awk -F= '$1 == "first_recv_rel_s" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  LAST_RECV_REL_S="$(awk -F= '$1 == "last_recv_rel_s" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  RECEIVER_INTEGRITY="$(awk -F= '$1 == "receiver_integrity" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  SOCKET_RXQ_DROPS="$(awk -F= '$1 == "socket_rxq_overflow_packets" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  RECEIVE_API_EFFECTIVE="$(awk -F= '$1 == "receive_api_effective" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  RECEIVE_CAPTURE_BYTES="$(awk -F= '$1 == "receive_capture_bytes" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  HEADER_ONLY_PACKETS="$(awk -F= '$1 == "header_only_packets" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  MISSING_KERNEL_TIMESTAMPS="$(awk -F= '$1 == "missing_kernel_timestamp_packets" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  CONTROL_TRUNCATED_PACKETS="$(awk -F= '$1 == "control_truncated_packets" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  CAPTURE_BACKPRESSURE_RECORDS="$(awk -F= '$1 == "capture_backpressure_records" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  DONE_RECEIVED="$(awk -F= '$1 == "done_received" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  [[ "$RECEIVED_PACKETS" =~ ^[0-9]+$ && "$RECEIVED_PACKETS" -gt 0 ]] || SERVER_PULL_RC=1
  [[ "$RECEIVER_INTEGRITY" == "clean" && "$SOCKET_RXQ_DROPS" == "0" ]] || SERVER_PULL_RC=1
  [[ "$MISSING_KERNEL_TIMESTAMPS" == "0" && "$CONTROL_TRUNCATED_PACKETS" == "0" && "$CAPTURE_BACKPRESSURE_RECORDS" == "0" ]] || SERVER_PULL_RC=1
  [[ "$DONE_RECEIVED" == "true" ]] || SERVER_PULL_RC=1
  [[ -z "$UDP_REQUIRE_RECEIVE_API" || "$RECEIVE_API_EFFECTIVE" == "$UDP_REQUIRE_RECEIVE_API" ]] || SERVER_PULL_RC=1
  if [[ "$RECEIVE_API_EFFECTIVE" == "recvmmsg" ]]; then
    [[ "$RECEIVE_CAPTURE_BYTES" == "256" && "$HEADER_ONLY_PACKETS" == "$RECEIVED_PACKETS" ]] || SERVER_PULL_RC=1
  fi
  if [[ "$POSTPROCESS" == "arrival" ]]; then
    [[ "$ARRIVAL_ROWS" == "$RECEIVED_PACKETS" ]] || SERVER_PULL_RC=1
    [[ "$PAYLOAD_BYTES_MIN" == "$PAYLOAD_BYTES" && "$PAYLOAD_BYTES_MAX" == "$PAYLOAD_BYTES" ]] || SERVER_PULL_RC=1
  fi
fi
if [[ "$RECEIVED_PACKETS" =~ ^[1-9][0-9]*$ &&
      "$FIRST_RECV_REL_S" =~ ^[0-9]+([.][0-9]+)?$ &&
      "$LAST_RECV_REL_S" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  AVERAGE_BANDWIDTH_MBPS="$(awk -v packets="$RECEIVED_PACKETS" -v payload="$PAYLOAD_BYTES" -v first="$FIRST_RECV_REL_S" -v last="$LAST_RECV_REL_S" '
    BEGIN {
      span = last - first
      if (span > 0) printf "%.9f", packets * payload * 8 / span / 1000000
    }')"
fi
if [[ ! "$ARRIVAL_MAX_MS" =~ ^[0-9]+$ ]] || (( ARRIVAL_MAX_MS < MIN_ARRIVAL_MAX_MS || ARRIVAL_MAX_MS > MAX_ARRIVAL_MAX_MS )); then
  [[ "$POSTPROCESS" == "arrival" ]] && SERVER_PULL_RC=1
fi

DELAY_ROWS=0
TIME_ROWS=0
DELAY_EMPTY_WINDOWS=""
RECV_SPAN_MS=""
if [[ "$POSTPROCESS" == "latency" ]]; then
  if [[ -s "$SERVER_DIR/delay.txt" ]]; then
    DELAY_ROWS="$(wc -l < "$SERVER_DIR/delay.txt" | tr -d ' ')"
    awk '$0 !~ /^-?[0-9]+$/ {bad=1} END {exit bad}' "$SERVER_DIR/delay.txt" || SERVER_PULL_RC=1
  else
    SERVER_PULL_RC=1
  fi
  if [[ -s "$RUN_DIR/delay_window_time_ms.txt" ]]; then
    TIME_ROWS="$(wc -l < "$RUN_DIR/delay_window_time_ms.txt" | tr -d ' ')"
    awk '$0 !~ /^[0-9]+([.][0-9]+)?$/ {bad=1} END {exit bad}' "$RUN_DIR/delay_window_time_ms.txt" || SERVER_PULL_RC=1
  else
    SERVER_PULL_RC=1
  fi
  if [[ -s "$SERVER_DIR/delay_summary.txt" ]]; then
    DELAY_EMPTY_WINDOWS="$(awk -F= '$1 == "empty_windows" {print $2; exit}' "$SERVER_DIR/delay_summary.txt")"
    RECV_SPAN_MS="$(awk -F= '$1 == "last_recv_rel_ms" {print $2; exit}' "$SERVER_DIR/delay_summary.txt")"
  else
    SERVER_PULL_RC=1
  fi
  if [[ -s "$SERVER_DIR/uplink_udp_receiver_summary.txt" ]]; then
    PAYLOAD_BYTES_MIN="$(awk -F= '$1 == "payload_bytes_min" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
    PAYLOAD_BYTES_MAX="$(awk -F= '$1 == "payload_bytes_max" {print $2; exit}' "$SERVER_DIR/uplink_udp_receiver_summary.txt")"
  fi
  [[ "$DELAY_ROWS" =~ ^[1-9][0-9]*$ ]] || SERVER_PULL_RC=1
  [[ "$TIME_ROWS" == "$DELAY_ROWS" ]] || SERVER_PULL_RC=1
  [[ "$RECV_SPAN_MS" =~ ^[0-9]+([.][0-9]+)?$ ]] || SERVER_PULL_RC=1
  awk -v span="$RECV_SPAN_MS" -v min="$MIN_RECV_SPAN_MS" -v max="$MAX_RECV_SPAN_MS" 'BEGIN { exit !(span >= min && span <= max) }' || SERVER_PULL_RC=1
  [[ "$PAYLOAD_BYTES_MIN" == "$PAYLOAD_BYTES" && "$PAYLOAD_BYTES_MAX" == "$PAYLOAD_BYTES" ]] || SERVER_PULL_RC=1
  if [[ -s "$SERVER_DIR/delay.txt" && -s "$SERVER_DIR/delay.txt.gz" && \
        -s "$RUN_DIR/delay_window_time_ms.txt" && -s "$SERVER_DIR/delay_summary.txt" && \
        -s "$SERVER_DIR/uplink_udp_receiver_summary.txt" && \
        "$DELAY_ROWS" =~ ^[1-9][0-9]*$ && "$TIME_ROWS" == "$DELAY_ROWS" && \
        "$RECV_SPAN_MS" =~ ^[0-9]+([.][0-9]+)?$ && \
        "$PAYLOAD_BYTES_MIN" == "$PAYLOAD_BYTES" && "$PAYLOAD_BYTES_MAX" == "$PAYLOAD_BYTES" ]] && \
     awk -v span="$RECV_SPAN_MS" -v min="$MIN_RECV_SPAN_MS" -v max="$MAX_RECV_SPAN_MS" 'BEGIN { exit !(span >= min && span <= max) }' && \
     awk '$0 !~ /^-?[0-9]+$/ {bad=1} END {exit bad}' "$SERVER_DIR/delay.txt" && \
     awk '$0 !~ /^[0-9]+([.][0-9]+)?$/ {bad=1} END {exit bad}' "$RUN_DIR/delay_window_time_ms.txt"; then
    SERVER_PULL_RC=0
  fi
fi

FINAL_RC=0
PLOT_RC=0
if [[ "$POSTPROCESS" == "arrival" && ! -s "$RUN_DIR/bandwidth_time_scatter.png" ]]; then
  PLOT_RC=1
fi
if [[ "$POSTPROCESS" == "latency" && ! -s "$RUN_DIR/time_delay_scatter.png" ]]; then
  PLOT_RC=1
fi
for rc in "$UDP_RC" "$SERVER_RC" "$UDP_PULL_RC" "$UDP_PROGRESS_PULL_RC" "$SERVER_PULL_RC" "$PROGRESS_AUDIT_RC" "$MONITOR_RC" "$PLOT_RC"; do [[ "$rc" -eq 0 ]] || FINAL_RC=1; done
if (( ICMP_ENABLED )); then
  for rc in "$ICMP_RC" "$ICMP_PULL_RC"; do [[ "$rc" -eq 0 ]] || FINAL_RC=1; done
fi
{
  if (( ICMP_ENABLED )); then
    printf 'icmp_status=%s\nicmp_rc=%s\nicmp_pull_rc=%s\n' "$([[ "$ICMP_RC" -eq 0 && "$ICMP_PULL_RC" -eq 0 ]] && echo complete || echo failed)" "$ICMP_RC" "$ICMP_PULL_RC"
  else
    printf 'icmp_status=skipped\nicmp_rc=skipped\nicmp_pull_rc=skipped\n'
  fi
  printf 'udp_rc=%s\nserver_rc=%s\nudp_pull_rc=%s\nudp_progress_pull_rc=%s\nserver_pull_rc=%s\nprogress_audit_rc=%s\nmonitor_rc=%s\nplot_rc=%s\n' "$UDP_RC" "$SERVER_RC" "$UDP_PULL_RC" "$UDP_PROGRESS_PULL_RC" "$SERVER_PULL_RC" "$PROGRESS_AUDIT_RC" "$MONITOR_RC" "$PLOT_RC"
} > "$RUN_DIR/exit_status.txt"
printf 'duration_mode=%s\nscheduled_packets=%s\nattempted_packets=%s\nsent_packets=%s\nsend_failed_packets=%s\nsender_elapsed_s=%s\nactual_payload_mbps=%s\ndeadline_reached=%s\narrival_rows=%s\nreceived_packets=%s\narrival_max_ms=%s\ndelay_rows=%s\ntime_rows=%s\ndelay_empty_windows=%s\nrecv_span_ms=%s\npayload_bytes_min=%s\npayload_bytes_max=%s\nclock_sync_samples=%s\n' "$DURATION_MODE" "$SCHEDULED_PACKETS" "$ATTEMPTED_PACKETS" "$SENT_PACKETS" "$SEND_FAILED_PACKETS" "$SENDER_ELAPSED_S" "$ACTUAL_PAYLOAD_MBPS" "$DEADLINE_REACHED" "$ARRIVAL_ROWS" "$RECEIVED_PACKETS" "$ARRIVAL_MAX_MS" "$DELAY_ROWS" "$TIME_ROWS" "$DELAY_EMPTY_WINDOWS" "$RECV_SPAN_MS" "$PAYLOAD_BYTES_MIN" "$PAYLOAD_BYTES_MAX" "$CLOCK_SYNC_SAMPLES" >> "$RUN_DIR/exit_status.txt"
printf 'receiver_integrity=%s\nsocket_rxq_overflow_packets=%s\nreceive_api_requested=%s\nreceive_api_effective=%s\nreceive_capture_bytes=%s\nheader_only_packets=%s\nmissing_kernel_timestamp_packets=%s\ncontrol_truncated_packets=%s\ncapture_backpressure_records=%s\ndone_received=%s\npayload_bytes_source=wire_length\n' "$RECEIVER_INTEGRITY" "$SOCKET_RXQ_DROPS" "$UDP_RECEIVE_API" "$RECEIVE_API_EFFECTIVE" "$RECEIVE_CAPTURE_BYTES" "$HEADER_ONLY_PACKETS" "$MISSING_KERNEL_TIMESTAMPS" "$CONTROL_TRUNCATED_PACKETS" "$CAPTURE_BACKPRESSURE_RECORDS" "$DONE_RECEIVED" >> "$RUN_DIR/exit_status.txt"
printf 'icmp_transmitted_packets=%s\nicmp_received_packets=%s\nicmp_packet_loss_percent=%s\n' "$ICMP_TRANSMITTED_PACKETS" "$ICMP_RECEIVED_PACKETS" "$ICMP_PACKET_LOSS_PERCENT" >> "$RUN_DIR/exit_status.txt"
printf 'average_bandwidth_mbps=%s\nicmp_transmitted_packets=%s\nicmp_received_packets=%s\nicmp_packet_loss_percent=%s\n' "$AVERAGE_BANDWIDTH_MBPS" "$ICMP_TRANSMITTED_PACKETS" "$ICMP_RECEIVED_PACKETS" "$ICMP_PACKET_LOSS_PERCENT" >> "$RUN_DIR/metadata.txt"
printf 'sender_progress_file=udp_phone/remote/uplink_udp_sender_progress.tsv\nreceiver_progress_file=server/remote/uplink_udp_receiver_progress.tsv\ngap_report_file=server/udp_receive_gap_report.tsv\ngap_audit_file=udp_receive_gap_audit.tsv\n' >> "$RUN_DIR/metadata.txt"
if [[ "$FINAL_RC" -eq 0 ]]; then
  echo 'status=complete' >> "$RUN_DIR/metadata.txt"
  if (( ICMP_ENABLED )); then echo 'icmp_status=complete' >> "$RUN_DIR/metadata.txt"; else echo 'icmp_status=skipped' >> "$RUN_DIR/metadata.txt"; fi
  adb -s "$UDP_DEVICE" shell "rm -rf '$PHONE_REMOTE_ROOT'" >/dev/null 2>&1 || true
  if (( ICMP_ENABLED )); then adb -s "$ICMP_DEVICE" shell "rm -rf '$PHONE_REMOTE_ROOT'" >/dev/null 2>&1 || true; fi
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "rm -rf '$SERVER_REMOTE_DIR'" >/dev/null 2>&1 || true
else
  echo 'status=failed' >> "$RUN_DIR/metadata.txt"
  if (( ICMP_ENABLED )); then echo 'icmp_status=failed' >> "$RUN_DIR/metadata.txt"; else echo 'icmp_status=skipped' >> "$RUN_DIR/metadata.txt"; fi
  printf 'remote_server_dir=%s\n' "$SERVER_REMOTE_DIR" >> "$RUN_DIR/metadata.txt"
  adb -s "$UDP_DEVICE" shell "rm -rf '$PHONE_REMOTE_ROOT'" >/dev/null 2>&1 || true
  if (( ICMP_ENABLED )); then adb -s "$ICMP_DEVICE" shell "rm -rf '$PHONE_REMOTE_ROOT'" >/dev/null 2>&1 || true; fi
fi
printf '%s\n' "$FINAL_RC" > "$RUN_DIR/exit_code.txt"
echo "Custom UDP uplink logs: $RUN_DIR"
exit "$FINAL_RC"
