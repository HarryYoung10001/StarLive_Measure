#!/usr/bin/env bash
set -euo pipefail

# Repeat one custom UDP uplink configuration, optionally with ICMP.

usage() {
  cat <<'USAGE'
Usage:
  ADB_UDP_DEVICE=<serial> [ADB_ICMP_DEVICE=<serial>] \
    bash traces/scripts/_archived/run_custom_udp_latency_uplink_repeat.sh \
    [--no-icmp] [server-ip] [duration_s] [rate] [successful_runs]

ICMP is enabled by default. --no-icmp requires only ADB_UDP_DEVICE.
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
(( ${#POSITIONAL[@]} <= 4 )) || { echo "ERROR: too many positional arguments" >&2; usage >&2; exit 2; }

SERVER="${POSITIONAL[0]-${SERVER:-}}"
DURATION="${POSITIONAL[1]-150}"
RATE="${POSITIONAL[2]-100M}"
SUCCESS_TARGET="${POSITIONAL[3]-20}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-30}"
COOLDOWN_S="${COOLDOWN_S:-30}"
PREFLIGHT_WAIT_S="${PREFLIGHT_WAIT_S:-300}"
PREFLIGHT_POLL_S="${PREFLIGHT_POLL_S:-5}"
POSTPROCESS="${UDP_POSTPROCESS:-arrival}"
WINDOW_MS="${UDP_WINDOW_MS:-10}"
EXPECTED_WIFI_SSID="${EXPECTED_WIFI_SSID:-}"
EXPECTED_DEVICE_MODEL="${EXPECTED_DEVICE_MODEL:-}"
UDP_DEVICE="${ADB_UDP_DEVICE:-}"
ICMP_DEVICE="${ADB_ICMP_DEVICE:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SESSION_DIR="${TRACE_LOG_ROOT:-$REPO_DIR/traces/logs/custom_udp_latency_uplink_repeat_$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$SESSION_DIR"

[[ "$SUCCESS_TARGET" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: success target must be positive" >&2; exit 2; }
[[ "$MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: MAX_ATTEMPTS must be positive" >&2; exit 2; }
[[ "$MAX_ATTEMPTS" -ge "$SUCCESS_TARGET" ]] || { echo "ERROR: MAX_ATTEMPTS must be >= success target" >&2; exit 2; }
[[ "$COOLDOWN_S" =~ ^[0-9]+$ ]] || { echo "ERROR: COOLDOWN_S must be a non-negative integer" >&2; exit 2; }
[[ "$PREFLIGHT_WAIT_S" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: PREFLIGHT_WAIT_S must be positive" >&2; exit 2; }
[[ "$PREFLIGHT_POLL_S" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: PREFLIGHT_POLL_S must be positive" >&2; exit 2; }
[[ "$POSTPROCESS" == "arrival" || "$POSTPROCESS" == "latency" ]] || { echo "ERROR: UDP_POSTPROCESS must be arrival or latency" >&2; exit 2; }
[[ "$EXPECTED_WIFI_SSID" != *$'\n'* && "$EXPECTED_WIFI_SSID" != *$'\r'* && "$EXPECTED_WIFI_SSID" != *$'\t'* ]] || { echo "ERROR: EXPECTED_WIFI_SSID contains control whitespace" >&2; exit 2; }
[[ "$EXPECTED_DEVICE_MODEL" != *$'\n'* && "$EXPECTED_DEVICE_MODEL" != *$'\r'* && "$EXPECTED_DEVICE_MODEL" != *$'\t'* ]] || { echo "ERROR: EXPECTED_DEVICE_MODEL contains control whitespace" >&2; exit 2; }
[[ -n "$UDP_DEVICE" ]] || { echo "ERROR: ADB_UDP_DEVICE is required" >&2; exit 2; }
if (( ICMP_ENABLED )); then
  [[ -n "$ICMP_DEVICE" ]] || { echo "ERROR: ADB_ICMP_DEVICE is required unless --no-icmp is used" >&2; exit 2; }
fi

cat > "$SESSION_DIR/repeat_metadata.txt" <<META
type=custom_udp_latency_uplink_repeat
server=$SERVER
duration_s=$DURATION
rate=$RATE
payload_bytes=${UDP_PAYLOAD_BYTES:-1200}
postprocess=$POSTPROCESS
window_ms=$WINDOW_MS
success_target=$SUCCESS_TARGET
max_attempts=$MAX_ATTEMPTS
expected_wifi_ssid=$EXPECTED_WIFI_SSID
expected_device_model=$EXPECTED_DEVICE_MODEL
icmp_enabled=$([[ "$ICMP_ENABLED" -eq 1 ]] && echo true || echo false)
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
META
printf 'event_no\tattempt\tsuccess_index\trun_id\tstart_utc\tend_utc\texit_code\tduration_mode\tscheduled_packets\tattempted_packets\tsent_packets\tsend_failed_packets\tsender_elapsed_s\tactual_payload_mbps\tdeadline_reached\treceived_packets\tarrival_rows\tarrival_max_ms\tdelay_rows\ttime_rows\tempty_windows\trecv_span_ms\tclock_sync_samples\tstatus\treason\ticmp_enabled\ticmp_status\ticmp_transmitted_packets\ticmp_received_packets\ticmp_packet_loss_percent\twifi_ssid\twifi_bssid\twifi_rssi\twifi_link_speed_mbps\tpreflight_file\n' > "$SESSION_DIR/repeat_status.tsv"
event_no=0
SUCCESS_COUNT=0
attempt=0
while [[ "$SUCCESS_COUNT" -lt "$SUCCESS_TARGET" && "$attempt" -lt "$MAX_ATTEMPTS" ]]; do
  PREFLIGHT_START="$(date +%s)"
  PREFLIGHT_LAST_LOG=0
  PREFLIGHT_OK=0
  while true; do
    ADB_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/custom_udp_repeat_adb.XXXXXX")"
    adb devices > "$ADB_SNAPSHOT" 2>&1 || true
    UDP_ABI_LIST="$(adb -s "$UDP_DEVICE" shell getprop ro.product.cpu.abilist 2>/dev/null | tr -d '\r' || true)"
    UDP_BOOT_COMPLETED="$(adb -s "$UDP_DEVICE" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
    UDP_MODEL="$(adb -s "$UDP_DEVICE" shell getprop ro.product.model 2>/dev/null | tr -d '\r' || true)"
    WIFI_LINE="$(adb -s "$UDP_DEVICE" shell dumpsys wifi 2>/dev/null | awk '/mWifiInfo SSID:/ {print; exit}' | tr -d '\r' || true)"
    WIFI_SSID="$(printf '%s\n' "$WIFI_LINE" | awk -F'SSID: ' 'NF > 1 {split($2, parts, ","); gsub(/[\"]/, "", parts[1]); print parts[1]; exit}')"
    WIFI_BSSID="$(printf '%s\n' "$WIFI_LINE" | sed -n 's/.*BSSID: \([^, ]*\).*/\1/p')"
    WIFI_RSSI="$(printf '%s\n' "$WIFI_LINE" | sed -n 's/.*RSSI: \(-*[0-9][0-9]*\).*/\1/p')"
    WIFI_LINK_SPEED="$(printf '%s\n' "$WIFI_LINE" | sed -n 's/.*Link speed: \([0-9][0-9]*\)Mbps.*/\1/p')"
    if (( ICMP_ENABLED )); then
      ICMP_BOOT_COMPLETED="$(adb -s "$ICMP_DEVICE" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
    else
      ICMP_BOOT_COMPLETED=1
    fi
    SERVER_READY=0
    ssh -o BatchMode=yes -o ConnectTimeout=5 "${SERVER_SSH:-sgp-server}" "test -d '${SERVER_REPO:-/root/Starlink_Measure}'" >/dev/null 2>&1 && SERVER_READY=1
    SERVER_PLATFORM="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "${SERVER_SSH:-sgp-server}" 'uname -s -m' 2>/dev/null | tr -d '\r' || true)"
    PORT_LISTENER="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "${SERVER_SSH:-sgp-server}" "ss -H -lun | awk '\$5 ~ /:${UDP_PORT:-5201}\$/ {print}'" 2>/dev/null || true)"
    EXPECTED_SSID_OK=1
    [[ -z "$EXPECTED_WIFI_SSID" || "$WIFI_SSID" == "$EXPECTED_WIFI_SSID" ]] || EXPECTED_SSID_OK=0
    EXPECTED_MODEL_OK=1
    [[ -z "$EXPECTED_DEVICE_MODEL" || "$UDP_MODEL" == "$EXPECTED_DEVICE_MODEL" ]] || EXPECTED_MODEL_OK=0
    if awk -v d="$UDP_DEVICE" '$1 == d && $2 == "device" { udp=1 } END { exit !(udp) }' "$ADB_SNAPSHOT" && \
       [[ "$UDP_ABI_LIST" == *arm* ]] && \
       [[ "$UDP_BOOT_COMPLETED" == "1" ]] && \
       [[ "$EXPECTED_SSID_OK" -eq 1 ]] && [[ "$EXPECTED_MODEL_OK" -eq 1 ]] && \
       { (( ICMP_ENABLED == 0 )) || awk -v d="$ICMP_DEVICE" '$1 == d && $2 == "device" { icmp=1 } END { exit !(icmp) }' "$ADB_SNAPSHOT"; } && \
       [[ "$ICMP_BOOT_COMPLETED" == "1" ]] && [[ "$SERVER_READY" -eq 1 ]] && \
       [[ "$SERVER_PLATFORM" == "Linux x86_64" ]] && [[ -z "$PORT_LISTENER" ]]; then
      PREFLIGHT_FILE="preflight_attempt_$(printf '%02d' "$((attempt + 1))").txt"
      printf 'timestamp_utc=%s\nstatus=pass\nadb_device=%s\nmodel=%s\nabi=%s\nboot_completed=%s\nwifi_ssid=%s\nwifi_bssid=%s\nwifi_rssi=%s\nwifi_link_speed_mbps=%s\nexpected_wifi_ssid=%s\nexpected_device_model=%s\nserver_ssh=%s\nserver_platform=%s\nserver_ready=%s\nudp_port=%s\nudp_port_listener=%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$UDP_DEVICE" "$UDP_MODEL" "$UDP_ABI_LIST" "$UDP_BOOT_COMPLETED" "$WIFI_SSID" "$WIFI_BSSID" "$WIFI_RSSI" "$WIFI_LINK_SPEED" "$EXPECTED_WIFI_SSID" "$EXPECTED_DEVICE_MODEL" "${SERVER_SSH:-sgp-server}" "$SERVER_PLATFORM" "$SERVER_READY" "${UDP_PORT:-5201}" "${PORT_LISTENER:-none}" > "$SESSION_DIR/$PREFLIGHT_FILE"
      PREFLIGHT_OK=1
      rm -f "$ADB_SNAPSHOT"
      break
    fi
    rm -f "$ADB_SNAPSHOT"
    NOW_EPOCH="$(date +%s)"
    if (( NOW_EPOCH - PREFLIGHT_START >= PREFLIGHT_WAIT_S )); then
      event_no=$((event_no + 1))
      NOW_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      PREFLIGHT_FILE="preflight_failed_$(date -u +%Y%m%dT%H%M%SZ).txt"
      printf 'timestamp_utc=%s\nstatus=failed\nreason=preflight timeout\nmodel=%s\nwifi_ssid=%s\nwifi_bssid=%s\nexpected_wifi_ssid=%s\nexpected_device_model=%s\nserver_platform=%s\nserver_ready=%s\nudp_port_listener=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$UDP_MODEL" "$WIFI_SSID" "$WIFI_BSSID" "$EXPECTED_WIFI_SSID" "$EXPECTED_DEVICE_MODEL" "$SERVER_PLATFORM" "$SERVER_READY" "${PORT_LISTENER:-none}" > "$SESSION_DIR/$PREFLIGHT_FILE"
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$event_no" "$attempt" "$SUCCESS_COUNT" "unknown" "$NOW_UTC" "$NOW_UTC" "2" \
        "" "" "" "" "" "" "" "" "" "" "" "" "" "" "" "" \
        "preflight_failed" "ADB/SSH/SSID/model/port preflight not ready" "$([[ "$ICMP_ENABLED" -eq 1 ]] && echo true || echo false)" "skipped" "" "" "" "$WIFI_SSID" "$WIFI_BSSID" "$WIFI_RSSI" "$WIFI_LINK_SPEED" "$PREFLIGHT_FILE" >> "$SESSION_DIR/repeat_status.tsv"
      echo "ERROR: preflight did not recover within ${PREFLIGHT_WAIT_S}s" >&2
      exit 1
    fi
    if (( NOW_EPOCH - PREFLIGHT_LAST_LOG >= 30 )); then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] waiting for preflight: model=${UDP_MODEL:-unknown} ssid=${WIFI_SSID:-unknown} expected=${EXPECTED_WIFI_SSID:-any} server_ready=$SERVER_READY port_listener=${PORT_LISTENER:-none}"
      PREFLIGHT_LAST_LOG="$NOW_EPOCH"
    fi
    sleep "$PREFLIGHT_POLL_S"
  done
  [[ "$PREFLIGHT_OK" -eq 1 ]] || exit 1
  attempt=$((attempt + 1))
  event_no=$((event_no + 1))
  START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  BEFORE_RUN="$(find "$SESSION_DIR" -maxdepth 1 -type d -name 'dual_udp_latency_uplink_*' -print | sort | tail -1)"
  echo "[$START_UTC] starting uplink attempt $attempt/$MAX_ATTEMPTS (success $SUCCESS_COUNT/$SUCCESS_TARGET)"
  set +e
  CHILD_ARGS=("$SERVER" "$DURATION" "$RATE" "${UDP_PORT:-5201}" "${UDP_PAYLOAD_BYTES:-1200}" "${PING_INTERVAL:-0.2}")
  if (( ICMP_ENABLED == 0 )); then CHILD_ARGS=(--no-icmp "${CHILD_ARGS[@]}"); fi
  TRACE_LOG_ROOT="$SESSION_DIR" \
  UDP_POSTPROCESS="$POSTPROCESS" \
  UDP_WINDOW_MS="$WINDOW_MS" \
  UDP_RECEIVE_API="${UDP_RECEIVE_API:-auto}" \
  UDP_RECEIVE_BATCH_SIZE="${UDP_RECEIVE_BATCH_SIZE:-64}" \
  UDP_RECV_BUFFER_BYTES="${UDP_RECV_BUFFER_BYTES:-67108864}" \
  UDP_MAX_RECEIVER_SOCKET_DROPS="${UDP_MAX_RECEIVER_SOCKET_DROPS:-0}" \
  UDP_REQUIRE_RECEIVE_API="${UDP_REQUIRE_RECEIVE_API:-}" \
  ADB_UDP_DEVICE="$UDP_DEVICE" \
  ADB_ICMP_DEVICE="$ICMP_DEVICE" \
  SERVER_SSH="${SERVER_SSH:-sgp-server}" \
  SERVER_REPO="${SERVER_REPO:-/root/Starlink_Measure}" \
  bash "$SCRIPT_DIR/run_custom_udp_latency_uplink.sh" "${CHILD_ARGS[@]}"
  rc=$?
  set -e
  END_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  RUN_DIR="$(find "$SESSION_DIR" -maxdepth 1 -type d -name 'dual_udp_latency_uplink_*' -print | sort | tail -1)"
  if [[ -z "$RUN_DIR" || "$RUN_DIR" == "$BEFORE_RUN" ]]; then RUN_DIR=""; fi
  DURATION_MODE=""; SCHEDULED_PACKETS=""; ATTEMPTED_PACKETS=""; SENT_PACKETS=""; SEND_FAILED_PACKETS=""; SENDER_ELAPSED_S=""; ACTUAL_PAYLOAD_MBPS=""; DEADLINE_REACHED=""; RECEIVED_PACKETS=""; ARRIVAL_ROWS=""; ARRIVAL_MAX_MS=""; DELAY_ROWS=""; TIME_ROWS=""; EMPTY_WINDOWS=""; RECV_SPAN_MS=""; CLOCK_SYNC_SAMPLES=""; ICMP_STATUS="skipped"; ICMP_TRANSMITTED_PACKETS=""; ICMP_RECEIVED_PACKETS=""; ICMP_PACKET_LOSS_PERCENT=""; reason="preflight failure"; status="failed"
  if [[ -n "$RUN_DIR" && -d "$RUN_DIR" ]]; then
    SCHEDULED_PACKETS="$(awk -F= '$1 == "scheduled_packets" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    DURATION_MODE="$(awk -F= '$1 == "duration_mode" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    ATTEMPTED_PACKETS="$(awk -F= '$1 == "attempted_packets" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    SENT_PACKETS="$(awk -F= '$1 == "sent_packets" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    SEND_FAILED_PACKETS="$(awk -F= '$1 == "send_failed_packets" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    SENDER_ELAPSED_S="$(awk -F= '$1 == "sender_elapsed_s" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    ACTUAL_PAYLOAD_MBPS="$(awk -F= '$1 == "actual_payload_mbps" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    DEADLINE_REACHED="$(awk -F= '$1 == "deadline_reached" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    RECEIVED_PACKETS="$(awk -F= '$1 == "received_packets" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    ARRIVAL_ROWS="$(awk -F= '$1 == "arrival_rows" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    ARRIVAL_MAX_MS="$(awk -F= '$1 == "arrival_max_ms" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    DELAY_ROWS="$(awk -F= '$1 == "delay_rows" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    TIME_ROWS="$(awk -F= '$1 == "time_rows" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    EMPTY_WINDOWS="$(awk -F= '$1 == "delay_empty_windows" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    RECV_SPAN_MS="$(awk -F= '$1 == "recv_span_ms" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    CLOCK_SYNC_SAMPLES="$(awk -F= '$1 == "clock_sync_samples" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    ICMP_STATUS="$(awk -F= '$1 == "icmp_status" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    ICMP_TRANSMITTED_PACKETS="$(awk -F= '$1 == "icmp_transmitted_packets" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    ICMP_RECEIVED_PACKETS="$(awk -F= '$1 == "icmp_received_packets" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    ICMP_PACKET_LOSS_PERCENT="$(awk -F= '$1 == "icmp_packet_loss_percent" {print $2; exit}' "$RUN_DIR/exit_status.txt" 2>/dev/null || true)"
    [[ -n "$ICMP_STATUS" ]] || ICMP_STATUS="$([[ "$ICMP_ENABLED" -eq 1 ]] && echo unknown || echo skipped)"
    if [[ "$rc" -eq 0 ]]; then
      SUCCESS_COUNT=$((SUCCESS_COUNT + 1)); status="complete"; reason="$([[ "$ICMP_ENABLED" -eq 1 ]] && echo 'validated UDP run and bandwidth scatter' || echo 'validated UDP-only run and bandwidth scatter')"
    else
      reason="runner exit $rc"
    fi
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$event_no" "$attempt" "$SUCCESS_COUNT" "$(basename "${RUN_DIR:-unknown}")" "$START_UTC" "$END_UTC" "$rc" "$DURATION_MODE" "$SCHEDULED_PACKETS" "$ATTEMPTED_PACKETS" "$SENT_PACKETS" "$SEND_FAILED_PACKETS" "$SENDER_ELAPSED_S" "$ACTUAL_PAYLOAD_MBPS" "$DEADLINE_REACHED" "$RECEIVED_PACKETS" "$ARRIVAL_ROWS" "$ARRIVAL_MAX_MS" "$DELAY_ROWS" "$TIME_ROWS" "$EMPTY_WINDOWS" "$RECV_SPAN_MS" "$CLOCK_SYNC_SAMPLES" "$status" "$reason" "$([[ "$ICMP_ENABLED" -eq 1 ]] && echo true || echo false)" "$ICMP_STATUS" "$ICMP_TRANSMITTED_PACKETS" "$ICMP_RECEIVED_PACKETS" "$ICMP_PACKET_LOSS_PERCENT" "$WIFI_SSID" "$WIFI_BSSID" "$WIFI_RSSI" "$WIFI_LINK_SPEED" "${PREFLIGHT_FILE:-}" >> "$SESSION_DIR/repeat_status.tsv"
  if [[ -n "$RUN_DIR" && "$COOLDOWN_S" -gt 0 ]]; then sleep "$COOLDOWN_S"; fi
done
echo "Processed custom UDP uplink session: $SESSION_DIR"

# Keep successful runs at the session root and move failed/incomplete attempts
# into explicit directories. The source names come from the status table and
# are constrained to direct children of SESSION_DIR.
FAILED_DIR="$SESSION_DIR/failed_attempts"
INCOMPLETE_DIR="$SESSION_DIR/incomplete_attempts"
mkdir -p "$FAILED_DIR" "$INCOMPLETE_DIR"
while IFS=$'\t' read -r row_event row_attempt row_success row_run row_start row_end row_rc row_mode row_scheduled row_attempted row_sent row_failed row_elapsed row_mbps row_deadline row_received row_arrival row_arrival_max row_delay row_time row_empty row_span row_clock row_status row_reason row_icmp row_icmp_status row_icmp_tx row_icmp_rx row_icmp_loss row_ssid row_bssid row_rssi row_link row_preflight; do
  [[ "$row_event" == "event_no" || "$row_event" =~ ^[0-9]+$ ]] || continue
  [[ "$row_run" != "" && "$row_run" != "unknown" ]] || continue
  SOURCE="$SESSION_DIR/$row_run"
  [[ -d "$SOURCE" ]] || continue
  TARGET_DIR=""
  if [[ "$row_status" == "failed" || "$row_rc" != "0" ]]; then
    TARGET_DIR="$FAILED_DIR"
  elif [[ "$row_status" == "incomplete" ]]; then
    TARGET_DIR="$INCOMPLETE_DIR"
  fi
  [[ -n "$TARGET_DIR" ]] || continue
  TARGET="$TARGET_DIR/$row_run"
  [[ ! -e "$TARGET" ]] || { echo "ERROR: archive target already exists: $TARGET" >&2; exit 1; }
  mv "$SOURCE" "$TARGET"
done < "$SESSION_DIR/repeat_status.tsv"

REPORT="$SESSION_DIR/session_report.tsv"
printf 'attempt\tsuccess_index\tstatus\texit_code\trun_path\tscheduled_packets\tsent_packets\treceived_packets\tactual_payload_mbps\tsocket_rxq_overflow_packets\treceive_api_effective\twifi_ssid\twifi_bssid\twifi_rssi\twifi_link_speed_mbps\treason\n' > "$REPORT"
while IFS=$'\t' read -r row_event row_attempt row_success row_run row_start row_end row_rc row_mode row_scheduled row_attempted row_sent row_failed row_elapsed row_mbps row_deadline row_received row_arrival row_arrival_max row_delay row_time row_empty row_span row_clock row_status row_reason row_icmp row_icmp_status row_icmp_tx row_icmp_rx row_icmp_loss row_ssid row_bssid row_rssi row_link row_preflight; do
  [[ "$row_event" =~ ^[0-9]+$ ]] || continue
  RUN_PATH=""
  for CANDIDATE in "$SESSION_DIR/$row_run" "$FAILED_DIR/$row_run" "$INCOMPLETE_DIR/$row_run"; do
    if [[ -d "$CANDIDATE" ]]; then RUN_PATH="${CANDIDATE#$SESSION_DIR/}"; break; fi
  done
  SOCKET_DROPS=""
  API_EFFECTIVE=""
  if [[ -n "$RUN_PATH" && -s "$SESSION_DIR/$RUN_PATH/exit_status.txt" ]]; then
    SOCKET_DROPS="$(awk -F= '$1 == "socket_rxq_overflow_packets" {print $2; exit}' "$SESSION_DIR/$RUN_PATH/exit_status.txt")"
    API_EFFECTIVE="$(awk -F= '$1 == "receive_api_effective" {print $2; exit}' "$SESSION_DIR/$RUN_PATH/exit_status.txt")"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$row_attempt" "$row_success" "$row_status" "$row_rc" "$RUN_PATH" "$row_scheduled" "$row_sent" "$row_received" "$row_mbps" "$SOCKET_DROPS" "$API_EFFECTIVE" "$row_ssid" "$row_bssid" "$row_rssi" "$row_link" "$row_reason" >> "$REPORT"
done < "$SESSION_DIR/repeat_status.tsv"

FINAL_STATUS="incomplete"
if [[ "$SUCCESS_COUNT" -eq "$SUCCESS_TARGET" ]]; then
  if [[ "$attempt" -eq "$SUCCESS_TARGET" ]]; then FINAL_STATUS="complete"; else FINAL_STATUS="complete_with_retries"; fi
fi
{
  printf 'status=%s\n' "$FINAL_STATUS"
  printf 'success_count=%s\nsuccess_target=%s\nattempt_count=%s\nmax_attempts=%s\nfailed_attempts=%s\n' "$SUCCESS_COUNT" "$SUCCESS_TARGET" "$attempt" "$MAX_ATTEMPTS" "$((attempt - SUCCESS_COUNT))"
  printf 'session_report=session_report.tsv\nrepeat_status=repeat_status.tsv\nfailed_attempts_dir=failed_attempts\nincomplete_attempts_dir=incomplete_attempts\n'
} > "$SESSION_DIR/session_summary.txt"
printf 'final_status=%s\nsuccess_count=%s\nattempt_count=%s\n' "$FINAL_STATUS" "$SUCCESS_COUNT" "$attempt" >> "$SESSION_DIR/repeat_metadata.txt"
if [[ "$FINAL_STATUS" == "complete" || "$FINAL_STATUS" == "complete_with_retries" ]]; then exit 0; fi
echo "ERROR: reached MAX_ATTEMPTS=$MAX_ATTEMPTS with only $SUCCESS_COUNT/$SUCCESS_TARGET successful runs" >&2
exit 1
