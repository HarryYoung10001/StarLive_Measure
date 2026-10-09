#!/usr/bin/env bash
set -euo pipefail

# Sequential rate sweep for the compact custom UDP uplink flow.

usage() {
  cat <<'USAGE'
Usage:
  SERVER_SSH=sgp-server ADB_UDP_DEVICE=<serial> \
    bash traces/scripts/_archived/run_custom_udp_latency_uplink_sweep.sh \
    [--no-icmp] [server-ip] [duration_s]

Defaults: rates=10M 20M 30M 40M 50M, duration_s=30, port=5201,
payload=1472, arrival window=100ms, wall-clock sender, no ICMP.
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

(( ${#POSITIONAL[@]} <= 2 )) || { echo "ERROR: too many positional arguments" >&2; usage >&2; exit 2; }

SERVER="${POSITIONAL[0]-${SERVER_IP:-${SERVER:-}}}"
DURATION="${POSITIONAL[1]-30}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SINGLE_RUNNER="$SCRIPT_DIR/run_custom_udp_latency_uplink.sh"
SERVER_SSH="${SERVER_SSH:-sgp-server}"
SERVER_REPO="${SERVER_REPO:-/root/Starlink_Measure}"
UDP_DEVICE="${ADB_UDP_DEVICE:-}"
PORT="${UDP_PORT:-5201}"
PAYLOAD_BYTES="${UDP_PAYLOAD_BYTES:-1472}"
PING_INTERVAL="${PING_INTERVAL:-0.2}"
POSTPROCESS="${UDP_POSTPROCESS:-arrival}"
WINDOW_MS="${UDP_WINDOW_MS:-100}"
DURATION_MODE="${UDP_DURATION_MODE:-wall-clock}"
RECEIVE_API="${UDP_RECEIVE_API:-auto}"
REQUIRE_RECEIVE_API="${UDP_REQUIRE_RECEIVE_API:-recvmmsg}"
BATCH_SIZE="${UDP_RECEIVE_BATCH_SIZE:-64}"
RECV_BUFFER_BYTES="${UDP_RECV_BUFFER_BYTES:-67108864}"
MAX_SOCKET_DROPS="${UDP_MAX_RECEIVER_SOCKET_DROPS:-0}"
EXPECTED_WIFI_SSID="${EXPECTED_WIFI_SSID:-STARLINK}"
EXPECTED_DEVICE_MODEL="${EXPECTED_DEVICE_MODEL:-RMX1941}"
PREFLIGHT_WAIT_S="${PREFLIGHT_WAIT_S:-900}"
PREFLIGHT_POLL_S="${PREFLIGHT_POLL_S:-5}"
MIN_SENDER_ELAPSED_S="${MIN_SENDER_ELAPSED_S:-29}"
MAX_SENDER_ELAPSED_S="${MAX_SENDER_ELAPSED_S:-35}"
MIN_ARRIVAL_MAX_MS="${MIN_ARRIVAL_MAX_MS:-29000}"
MAX_ARRIVAL_MAX_MS="${MAX_ARRIVAL_MAX_MS:-600000}"
DRY_RUN="${DRY_RUN:-0}"

fail() { echo "ERROR: $*" >&2; exit 2; }

if [[ -z "$SERVER" ]]; then
  SERVER="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" \
    "ip -4 route get 8.8.8.8 | awk '{for (i=1; i<=NF; i++) if (\$i==\"src\") {print \$(i+1); exit}}'" \
    2>/dev/null | tr -d '\r' | awk 'NF {print; exit}')"
fi
[[ -n "$SERVER" ]] || fail "server IP/hostname is required or could not be resolved"
[[ "$SERVER" =~ ^[A-Za-z0-9._:-]+$ ]] || fail "SERVER contains unsupported characters"
[[ "$DURATION" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "duration_s must be numeric"
awk -v value="$DURATION" 'BEGIN { exit !(value > 0) }' || fail "duration_s must be positive"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || fail "UDP_PORT must be in 1..65535"
[[ "$PAYLOAD_BYTES" =~ ^[1-9][0-9]*$ ]] && (( PAYLOAD_BYTES >= 48 )) || fail "UDP_PAYLOAD_BYTES must be >= 48"
[[ "$POSTPROCESS" == "arrival" ]] || fail "UDP_POSTPROCESS must be arrival for a bandwidth sweep"
[[ "$DURATION_MODE" == "wall-clock" ]] || fail "UDP_DURATION_MODE must be wall-clock for this sweep"
[[ "$RECEIVE_API" == "auto" || "$RECEIVE_API" == "readmsg" || "$RECEIVE_API" == "recvmmsg" ]] || fail "UDP_RECEIVE_API is invalid"
[[ "$REQUIRE_RECEIVE_API" == "readmsg" || "$REQUIRE_RECEIVE_API" == "recvmmsg" ]] || fail "UDP_REQUIRE_RECEIVE_API is invalid"
[[ "$BATCH_SIZE" =~ ^[1-9][0-9]*$ && "$BATCH_SIZE" -le 256 ]] || fail "UDP_RECEIVE_BATCH_SIZE must be 1..256"
[[ "$RECV_BUFFER_BYTES" =~ ^[0-9]+$ && "$RECV_BUFFER_BYTES" -ge 65536 ]] || fail "UDP_RECV_BUFFER_BYTES must be >= 65536"
[[ "$MAX_SOCKET_DROPS" =~ ^[0-9]+$ ]] || fail "UDP_MAX_RECEIVER_SOCKET_DROPS must be non-negative"
[[ "$PREFLIGHT_WAIT_S" =~ ^[1-9][0-9]*$ ]] || fail "PREFLIGHT_WAIT_S must be positive"
[[ "$PREFLIGHT_POLL_S" =~ ^[1-9][0-9]*$ ]] || fail "PREFLIGHT_POLL_S must be positive"
[[ "$MIN_SENDER_ELAPSED_S" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "MIN_SENDER_ELAPSED_S must be numeric"
[[ "$MAX_SENDER_ELAPSED_S" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "MAX_SENDER_ELAPSED_S must be numeric"
[[ "$MIN_ARRIVAL_MAX_MS" =~ ^[0-9]+$ ]] || fail "MIN_ARRIVAL_MAX_MS must be an integer"
[[ "$MAX_ARRIVAL_MAX_MS" =~ ^[0-9]+$ ]] || fail "MAX_ARRIVAL_MAX_MS must be an integer"
(( ICMP_ENABLED == 0 )) || fail "uplink rate sweep requires --no-icmp"
[[ -n "$UDP_DEVICE" ]] || fail "ADB_UDP_DEVICE is required"
[[ "$DRY_RUN" == "0" || "$DRY_RUN" == "1" ]] || fail "DRY_RUN must be 0 or 1"

if [[ -n "${UDP_RATES:-}" ]]; then
  read -r -a RATES <<< "$UDP_RATES"
else
  RATES=(10M 20M 30M 40M 50M)
fi
(( ${#RATES[@]} == 5 )) || fail "UDP_RATES must contain exactly five rates"
for rate in "${RATES[@]}"; do
  [[ "$rate" =~ ^[0-9]+([.][0-9]+)?([KMG]([bB][pP][sS])?|pps)?$ ]] || fail "invalid UDP rate: $rate"
done

SESSION_NAME="${TRACE_SESSION_NAME:-starlink_uplink_rate_sweep_30s_$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$SESSION_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || fail "invalid TRACE_SESSION_NAME"
SESSION_DIR="${TRACE_LOG_ROOT:-$REPO_DIR/traces/logs/$SESSION_NAME}"
if [[ -e "$SESSION_DIR" ]]; then
  [[ -d "$SESSION_DIR" ]] || fail "TRACE_LOG_ROOT is not a directory: $SESSION_DIR"
  [[ -z "$(find "$SESSION_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] || fail "TRACE_LOG_ROOT must be empty: $SESSION_DIR"
fi
mkdir -p "$SESSION_DIR"

cat > "$SESSION_DIR/sweep_metadata.txt" <<META
session_name=$SESSION_NAME
server=$SERVER
server_ssh=$SERVER_SSH
server_repo=$SERVER_REPO
udp_direction=uplink
flow_type=custom_timestamped_udp
rates=${RATES[*]}
duration_s=$DURATION
duration_mode=$DURATION_MODE
udp_port=$PORT
udp_payload_len=$PAYLOAD_BYTES
postprocess=$POSTPROCESS
window_ms=$WINDOW_MS
receive_api_requested=$RECEIVE_API
require_receive_api=$REQUIRE_RECEIVE_API
receive_batch_size=$BATCH_SIZE
recv_buffer_bytes=$RECV_BUFFER_BYTES
max_receiver_socket_drops=$MAX_SOCKET_DROPS
expected_wifi_ssid=$EXPECTED_WIFI_SSID
expected_device_model=$EXPECTED_DEVICE_MODEL
udp_device=$UDP_DEVICE
icmp_enabled=false
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
META
printf 'rate\tstart_utc\tend_utc\texit_code\tstatus\tnetwork_changed\trun_id\tpreflight_file\tpostflight_file\n' > "$SESSION_DIR/sweep_status.tsv"
printf 'rate\trun_id\tstatus\texit_code\tsender_elapsed_s\tactual_payload_mbps\tscheduled_packets\tsent_packets\tsend_failed_packets\treceived_packets\tarrival_rows\tarrival_max_ms\treceiver_integrity\tsocket_rxq_overflow_packets\treceive_api_effective\tapp_read_lag_ms_p95\tnetwork_changed\ttrace_path\n' > "$SESSION_DIR/sweep_summary.tsv"

if [[ "$DRY_RUN" == "1" ]]; then
  for rate in "${RATES[@]}"; do
    printf '%s\tDRY_RUN\tdry_run\t0\t\t\t\t\t\t\t\t\t\t\t\t\tfalse\t\n' "$rate" >> "$SESSION_DIR/sweep_summary.tsv"
    printf '%s\tdry-run\tdry-run\t0\tdry_run\tfalse\tdry_run\t\t\t\n' "$rate" >> "$SESSION_DIR/sweep_status.tsv"
  done
  printf 'status=dry_run\nrates=%s\n' "${RATES[*]}" > "$SESSION_DIR/session_summary.txt"
  exit 0
fi

for command in adb ssh scp go python3; do
  command -v "$command" >/dev/null 2>&1 || fail "$command is required"
done

read_wifi() {
  WIFI_LINE="$(adb -s "$UDP_DEVICE" shell dumpsys wifi 2>/dev/null | awk '/mWifiInfo SSID:/ {print; exit}' | tr -d '\r' || true)"
  WIFI_SSID="$(printf '%s\n' "$WIFI_LINE" | awk -F'SSID: ' 'NF > 1 {split($2, parts, ","); gsub(/[\"]/, "", parts[1]); print parts[1]; exit}')"
  WIFI_BSSID="$(printf '%s\n' "$WIFI_LINE" | sed -n 's/.*BSSID: \([^, ]*\).*/\1/p')"
  WIFI_RSSI="$(printf '%s\n' "$WIFI_LINE" | sed -n 's/.*RSSI: \(-*[0-9][0-9]*\).*/\1/p')"
  WIFI_LINK_SPEED="$(printf '%s\n' "$WIFI_LINE" | sed -n 's/.*Link speed: \([0-9][0-9]*\)Mbps.*/\1/p')"
}

write_preflight() {
  local path="$1" status="$2" reason="$3"
  printf 'timestamp_utc=%s\nstatus=%s\nreason=%s\nadb_device=%s\nmodel=%s\nabi=%s\nboot_completed=%s\nwifi_ssid=%s\nwifi_bssid=%s\nwifi_rssi=%s\nwifi_link_speed_mbps=%s\nexpected_wifi_ssid=%s\nexpected_device_model=%s\nserver_ssh=%s\nserver_platform=%s\nserver_ready=%s\nudp_port=%s\nudp_port_listener=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$status" "$reason" "$UDP_DEVICE" "${UDP_MODEL:-}" "${UDP_ABI_LIST:-}" "${UDP_BOOT_COMPLETED:-}" "$WIFI_SSID" "$WIFI_BSSID" "$WIFI_RSSI" "$WIFI_LINK_SPEED" "$EXPECTED_WIFI_SSID" "$EXPECTED_DEVICE_MODEL" "$SERVER_SSH" "${SERVER_PLATFORM:-}" "${SERVER_READY:-0}" "$PORT" "${PREFLIGHT_SERVER_LISTENER:-none}" > "$path"
}

preflight() {
  local start now snapshot
  start="$(date +%s)"
  while true; do
    snapshot="$(mktemp "${TMPDIR:-/tmp}/uplink_sweep_adb.XXXXXX")"
    adb devices > "$snapshot" 2>&1 || true
    UDP_ABI_LIST="$(adb -s "$UDP_DEVICE" shell getprop ro.product.cpu.abilist 2>/dev/null | tr -d '\r' || true)"
    UDP_BOOT_COMPLETED="$(adb -s "$UDP_DEVICE" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
    UDP_MODEL="$(adb -s "$UDP_DEVICE" shell getprop ro.product.model 2>/dev/null | tr -d '\r' || true)"
    read_wifi
    SERVER_READY=0
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "test -d '$SERVER_REPO'" >/dev/null 2>&1 && SERVER_READY=1
    SERVER_PLATFORM="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" 'uname -s -m' 2>/dev/null | tr -d '\r' || true)"
    PREFLIGHT_SERVER_LISTENER="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "ss -H -lun | awk '\$5 ~ /:${PORT}\$/ {print}'" 2>/dev/null || true)"
    if awk -v d="$UDP_DEVICE" '$1 == d && $2 == "device" {ok=1} END {exit !ok}' "$snapshot" && \
       [[ "$UDP_ABI_LIST" == *arm* ]] && [[ "$UDP_BOOT_COMPLETED" == "1" ]] && \
       [[ "$UDP_MODEL" == "$EXPECTED_DEVICE_MODEL" ]] && [[ "$WIFI_SSID" == "$EXPECTED_WIFI_SSID" ]] && \
       [[ "$SERVER_READY" -eq 1 ]] && [[ "$SERVER_PLATFORM" == "Linux x86_64" ]] && [[ -z "$PREFLIGHT_SERVER_LISTENER" ]]; then
      rm -f "$snapshot"
      return 0
    fi
    rm -f "$snapshot"
    now="$(date +%s)"
    if (( now - start >= PREFLIGHT_WAIT_S )); then
      PREFLIGHT_REASON="preflight timeout"
      return 1
    fi
    sleep "$PREFLIGHT_POLL_S"
  done
}

read_run_value() {
  local file="$1" key="$2"
  awk -F= -v key="$key" '$1 == key {print $2; exit}' "$file" 2>/dev/null || true
}

rate_index=0
failed_rates=0
stop_reason=""
for rate in "${RATES[@]}"; do
  rate_index=$((rate_index + 1))
  preflight_file="preflight_rate_$(printf '%02d' "$rate_index").txt"
  postflight_file="postflight_rate_$(printf '%02d' "$rate_index").txt"
  if ! preflight; then
    write_preflight "$SESSION_DIR/$preflight_file" failed "${PREFLIGHT_REASON:-preflight timeout}"
    now_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s\t%s\t%s\t1\tpreflight_failed\tfalse\tunknown\t%s\t\n' "$rate" "$now_utc" "$now_utc" "$preflight_file" >> "$SESSION_DIR/sweep_status.tsv"
    stop_reason="preflight timeout before $rate"
    break
  fi
  write_preflight "$SESSION_DIR/$preflight_file" pass ready
  preflight_bssid="$WIFI_BSSID"
  start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  before_run="$(find "$SESSION_DIR" -maxdepth 1 -type d -name 'dual_udp_latency_uplink_*' -print | sort | tail -1)"
  echo "[$start_utc] starting uplink rate $rate"
  set +e
  TRACE_LOG_ROOT="$SESSION_DIR" \
  ADB_UDP_DEVICE="$UDP_DEVICE" \
  SERVER_SSH="$SERVER_SSH" \
  SERVER_REPO="$SERVER_REPO" \
  UDP_POSTPROCESS="$POSTPROCESS" \
  UDP_WINDOW_MS="$WINDOW_MS" \
  UDP_DURATION_MODE="$DURATION_MODE" \
  UDP_RECEIVE_API="$RECEIVE_API" \
  UDP_REQUIRE_RECEIVE_API="$REQUIRE_RECEIVE_API" \
  UDP_RECEIVE_BATCH_SIZE="$BATCH_SIZE" \
  UDP_RECV_BUFFER_BYTES="$RECV_BUFFER_BYTES" \
  UDP_MAX_RECEIVER_SOCKET_DROPS="$MAX_SOCKET_DROPS" \
  UDP_PORT="$PORT" \
  UDP_PAYLOAD_BYTES="$PAYLOAD_BYTES" \
  MIN_SENDER_ELAPSED_S="$MIN_SENDER_ELAPSED_S" \
  MAX_SENDER_ELAPSED_S="$MAX_SENDER_ELAPSED_S" \
  MIN_ARRIVAL_MAX_MS="$MIN_ARRIVAL_MAX_MS" \
  MAX_ARRIVAL_MAX_MS="$MAX_ARRIVAL_MAX_MS" \
  bash "$SINGLE_RUNNER" --no-icmp "$SERVER" "$DURATION" "$rate" "$PORT" "$PAYLOAD_BYTES" "$PING_INTERVAL"
  run_rc=$?
  set -e
  end_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  RUN_DIR="$(find "$SESSION_DIR" -maxdepth 1 -type d -name 'dual_udp_latency_uplink_*' -print | sort | tail -1)"
  [[ -n "$RUN_DIR" && "$RUN_DIR" != "$before_run" ]] || RUN_DIR=""

  read_wifi
  write_preflight "$SESSION_DIR/$postflight_file" pass postflight
  network_changed=false
  if [[ "$UDP_MODEL" != "$EXPECTED_DEVICE_MODEL" || "$WIFI_SSID" != "$EXPECTED_WIFI_SSID" || "$WIFI_BSSID" != "$preflight_bssid" ]]; then
    network_changed=true
  fi

  run_id="$(basename "${RUN_DIR:-unknown}")"
  status=failed
  sender_elapsed=""
  if [[ -n "$RUN_DIR" ]]; then
    sender_elapsed="$(read_run_value "$RUN_DIR/exit_status.txt" sender_elapsed_s)"
  fi
  if [[ "$network_changed" == true ]]; then
    status=contaminated_network_change
  elif [[ "$run_rc" -eq 0 ]]; then
    status=complete
  elif [[ "$sender_elapsed" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v value="$sender_elapsed" -v max="$MAX_SENDER_ELAPSED_S" 'BEGIN {exit !(value > max)}'; then
    status=sender_backpressured
  fi
  [[ "$status" == complete ]] || failed_rates=$((failed_rates + 1))

  actual_mbps=""; scheduled=""; sent=""; send_failed=""; received=""; arrival_rows=""; arrival_max=""; integrity=""; socket_drops=""; api_effective=""; app_lag=""
  if [[ -n "$RUN_DIR" ]]; then
    exit_file="$RUN_DIR/exit_status.txt"
    summary_file="$RUN_DIR/server/uplink_udp_receiver_summary.txt"
    actual_mbps="$(read_run_value "$exit_file" actual_payload_mbps)"
    scheduled="$(read_run_value "$exit_file" scheduled_packets)"
    sent="$(read_run_value "$exit_file" sent_packets)"
    send_failed="$(read_run_value "$exit_file" send_failed_packets)"
    received="$(read_run_value "$exit_file" received_packets)"
    arrival_rows="$(read_run_value "$exit_file" arrival_rows)"
    arrival_max="$(read_run_value "$exit_file" arrival_max_ms)"
    integrity="$(read_run_value "$exit_file" receiver_integrity)"
    socket_drops="$(read_run_value "$exit_file" socket_rxq_overflow_packets)"
    api_effective="$(read_run_value "$exit_file" receive_api_effective)"
    app_lag="$(read_run_value "$summary_file" app_read_lag_ms_p95)"
  fi
  trace_path="${RUN_DIR#$SESSION_DIR/}"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$rate" "$start_utc" "$end_utc" "$run_rc" "$status" "$network_changed" "$run_id" "$preflight_file" "$postflight_file" >> "$SESSION_DIR/sweep_status.tsv"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$rate" "$run_id" "$status" "$run_rc" "$sender_elapsed" "$actual_mbps" "$scheduled" "$sent" "$send_failed" "$received" "$arrival_rows" "$arrival_max" "$integrity" "$socket_drops" "$api_effective" "$app_lag" "$network_changed" "$trace_path" >> "$SESSION_DIR/sweep_summary.tsv"
done

if [[ -n "$stop_reason" ]]; then
  final_status=preflight_failed
elif [[ "$failed_rates" -eq 0 && "$rate_index" -eq "${#RATES[@]}" ]]; then
  final_status=complete
else
  final_status=completed_with_failures
fi
{
  printf 'status=%s\n' "$final_status"
  printf 'rates_total=%s\nrates_attempted=%s\nrates_failed=%s\n' "${#RATES[@]}" "$rate_index" "$failed_rates"
  printf 'rates=%s\nduration_s=%s\nduration_mode=%s\n' "${RATES[*]}" "$DURATION" "$DURATION_MODE"
  [[ -z "$stop_reason" ]] || printf 'stop_reason=%s\n' "$stop_reason"
  printf 'sweep_status=sweep_status.tsv\nsweep_summary=sweep_summary.tsv\n'
} > "$SESSION_DIR/session_summary.txt"
printf 'final_status=%s\nrates_attempted=%s\nrates_failed=%s\n' "$final_status" "$rate_index" "$failed_rates" >> "$SESSION_DIR/sweep_metadata.txt"
echo "Uplink rate sweep outputs saved to: $SESSION_DIR"
if [[ "$final_status" == complete ]]; then exit 0; fi
exit 1
