#!/usr/bin/env bash
set -euo pipefail

# Repeat one compact custom UDP downlink configuration until enough runs pass validation.

usage() {
  cat <<'USAGE'
Usage:
  ADB_UDP_DEVICE=<serial> [ADB_ICMP_DEVICE=<serial>] \
    bash traces/scripts/_archived/run_custom_udp_latency_downlink_repeat.sh \
    [--no-icmp] [server-ip] [duration_s] [rate] [successful_runs]

Defaults: duration_s=30 rate=200M successful_runs=20
UDP_POSTPROCESS=arrival|latency selects the compact trace type.
EXPECTED_WIFI_SSID optionally requires a matching Android Wi-Fi SSID before each attempt.
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
(( ${#POSITIONAL[@]} <= 4 )) || { echo "ERROR: too many positional arguments" >&2; exit 2; }

SERVER="${POSITIONAL[0]-${SERVER:-}}"
DURATION="${POSITIONAL[1]-30}"
RATE="${POSITIONAL[2]-200M}"
SUCCESS_TARGET="${POSITIONAL[3]-20}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-30}"
COOLDOWN_S="${COOLDOWN_S:-30}"
PREFLIGHT_WAIT_S="${PREFLIGHT_WAIT_S:-300}"
PREFLIGHT_POLL_S="${PREFLIGHT_POLL_S:-5}"
POSTPROCESS="${UDP_POSTPROCESS:-arrival}"
WINDOW_MS="${UDP_WINDOW_MS:-10}"
DRY_RUN="${DRY_RUN:-0}"
EXPECTED_WIFI_SSID="${EXPECTED_WIFI_SSID:-}"
UDP_PORT="${UDP_PORT:-5201}"
UDP_PAYLOAD_BYTES="${UDP_PAYLOAD_BYTES:-1200}"
UDP_DEVICE="${ADB_UDP_DEVICE:-}"
ICMP_DEVICE="${ADB_ICMP_DEVICE:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SESSION_DIR="${TRACE_LOG_ROOT:-$REPO_DIR/traces/logs/custom_udp_latency_downlink_repeat_$(date -u +%Y%m%dT%H%M%SZ)}"

for value in "$SUCCESS_TARGET" "$MAX_ATTEMPTS" "$PREFLIGHT_WAIT_S" "$PREFLIGHT_POLL_S"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: repeat counts/timeouts must be positive integers" >&2; exit 2; }
done
[[ "$MAX_ATTEMPTS" -ge "$SUCCESS_TARGET" ]] || { echo "ERROR: MAX_ATTEMPTS must be >= successful_runs" >&2; exit 2; }
[[ "$COOLDOWN_S" =~ ^[0-9]+$ ]] || { echo "ERROR: COOLDOWN_S must be non-negative" >&2; exit 2; }
[[ "$EXPECTED_WIFI_SSID" != *$'\n'* && "$EXPECTED_WIFI_SSID" != *$'\r'* && "$EXPECTED_WIFI_SSID" != *$'\t'* ]] || { echo "ERROR: EXPECTED_WIFI_SSID contains control whitespace" >&2; exit 2; }
[[ "$POSTPROCESS" == "arrival" || "$POSTPROCESS" == "latency" ]] || { echo "ERROR: UDP_POSTPROCESS must be arrival or latency" >&2; exit 2; }
[[ "$DRY_RUN" == "0" || "$DRY_RUN" == "1" ]] || { echo "ERROR: DRY_RUN must be 0 or 1" >&2; exit 2; }
if [[ -z "$SERVER" && "$DRY_RUN" == "0" ]]; then
  SERVER="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "${SERVER_SSH:-sgp-server}" "ip -4 route get 8.8.8.8 | awk '{for (i=1; i<=NF; i++) if (\$i==\"src\") {print \$(i+1); exit}}'" 2>/dev/null | tr -d '\r' | awk 'NF {print; exit}')"
fi
[[ -n "$SERVER" ]] || { echo "ERROR: server IP is required or could not be resolved" >&2; exit 2; }
if [[ "$DRY_RUN" == "0" ]]; then
  [[ -n "$UDP_DEVICE" ]] || { echo "ERROR: ADB_UDP_DEVICE is required" >&2; exit 2; }
  if (( ICMP_ENABLED )); then [[ -n "$ICMP_DEVICE" ]] || { echo "ERROR: ADB_ICMP_DEVICE is required unless --no-icmp is used" >&2; exit 2; }; fi
fi

mkdir -p "$SESSION_DIR"
if [[ -f "$SESSION_DIR/repeat_status.tsv" ]]; then
  status_fields="$(awk -F'\t' 'NR == 1 {print NF; exit}' "$SESSION_DIR/repeat_status.tsv")"
  [[ "$status_fields" == "29" ]] || { echo "ERROR: existing repeat_status.tsv has an incompatible schema" >&2; exit 2; }
  [[ -f "$SESSION_DIR/repeat_metadata.txt" ]] || { echo "ERROR: existing session is missing repeat_metadata.txt" >&2; exit 2; }
  printf 'resumed_utc=%s\nresume_device=%s\nresume_expected_wifi_ssid=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$UDP_DEVICE" "$EXPECTED_WIFI_SSID" >> "$SESSION_DIR/repeat_metadata.txt"
else
  cat > "$SESSION_DIR/repeat_metadata.txt" <<META
type=custom_udp_latency_downlink_repeat
server=$SERVER
duration_s=$DURATION
rate=$RATE
postprocess=$POSTPROCESS
window_ms=$WINDOW_MS
success_target=$SUCCESS_TARGET
max_attempts=$MAX_ATTEMPTS
expected_wifi_ssid=$EXPECTED_WIFI_SSID
udp_port=$UDP_PORT
udp_payload_bytes=$UDP_PAYLOAD_BYTES
icmp_enabled=$([[ "$ICMP_ENABLED" -eq 1 ]] && echo true || echo false)
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
META
  printf 'attempt\tsuccess_index\trun_id\tstart_utc\tend_utc\texit_code\tduration_mode\tscheduled_packets\tattempted_packets\tsent_packets\tsend_failed_packets\tsender_elapsed_s\tactual_payload_mbps\treceived_packets\tarrival_rows\tdelay_rows\ttime_rows\tempty_windows\trecv_span_ms\tclock_sync_samples\tstatus\treason\ticmp_enabled\ticmp_status\twifi_ssid\twifi_bssid\twifi_rssi\twifi_link_speed_mbps\tpreflight_file\n' > "$SESSION_DIR/repeat_status.tsv"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  child_args=("$SERVER" "$DURATION" "$RATE" "$UDP_PORT" "$UDP_PAYLOAD_BYTES" "${PING_INTERVAL:-0.2}")
  if (( ICMP_ENABLED == 0 )); then child_args=(--no-icmp "${child_args[@]}"); fi
  TRACE_LOG_ROOT="$SESSION_DIR" UDP_POSTPROCESS="$POSTPROCESS" UDP_WINDOW_MS="$WINDOW_MS" DRY_RUN=1 \
    bash "$SCRIPT_DIR/run_custom_udp_latency_downlink.sh" "${child_args[@]}"
  echo "Dry run complete: $SESSION_DIR"
  exit 0
fi

success_count="$(awk -F'\t' 'NR > 1 && $21 == "complete" {n++} END {print n + 0}' "$SESSION_DIR/repeat_status.tsv")"
attempt="$(awk -F'\t' 'NR > 1 && NF {n++} END {print n + 0}' "$SESSION_DIR/repeat_status.tsv")"

write_preflight_snapshot() {
  local file="$1"
  local status="$2"
  local reason="$3"
  printf 'timestamp_utc=%s\nstatus=%s\nreason=%s\nadb_device=%s\nadb_state=%s\nboot_completed=%s\nabi=%s\nmodel=%s\nwifi_ssid=%s\nwifi_bssid=%s\nwifi_rssi=%s\nwifi_link_speed_mbps=%s\nwifi_info=%s\nexpected_wifi_ssid=%s\nserver_ssh=%s\nserver_repo=%s\nserver_ready=%s\nudp_port=%s\nudp_port_listener=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$status" "$reason" "$UDP_DEVICE" "$adb_state" "$udp_boot" "$udp_abi" "$udp_model" "$wifi_ssid" "$wifi_ssid_bssid" "$wifi_rssi" "$wifi_link_speed" "$wifi_line" "$EXPECTED_WIFI_SSID" "${SERVER_SSH:-sgp-server}" "${SERVER_REPO:-/root/Starlink_Measure}" "$server_ready" "$UDP_PORT" "${port_listener:-none}" > "$SESSION_DIR/$file"
}

append_status_row() {
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$attempt" "$success_count" "$(basename "${run_dir:-unknown}")" "$start_utc" "$end_utc" "$rc" "$duration_mode" "$scheduled" "$attempted" "$sent" "$failed" "$elapsed" "$actual" "$received" "$arrivals" "$delays" "$times" "$empty" "$span" "$clocks" "$status" "$reason" "$([[ "$ICMP_ENABLED" -eq 1 ]] && echo true || echo false)" "$icmp_status" "$wifi_ssid" "$wifi_ssid_bssid" "$wifi_rssi" "$wifi_link_speed" "$preflight_file" >> "$SESSION_DIR/repeat_status.tsv"
}

while [[ "$success_count" -lt "$SUCCESS_TARGET" && "$attempt" -lt "$MAX_ATTEMPTS" ]]; do
  preflight_start="$(date +%s)"
  PREFLIGHT_LAST_LOG=0
  while true; do
    snapshot="$(mktemp "${TMPDIR:-/tmp}/downlink_repeat_adb.XXXXXX")"
    adb devices > "$snapshot" 2>&1 || true
    adb_state="$(awk -v d="$UDP_DEVICE" '$1==d {print $2; exit}' "$snapshot")"
    udp_abi="$(adb -s "$UDP_DEVICE" shell getprop ro.product.cpu.abilist 2>/dev/null | tr -d '\r' || true)"
    udp_boot="$(adb -s "$UDP_DEVICE" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
    udp_model="$(adb -s "$UDP_DEVICE" shell getprop ro.product.model 2>/dev/null | tr -d '\r' || true)"
    wifi_line="$(adb -s "$UDP_DEVICE" shell dumpsys wifi 2>/dev/null | awk '/mWifiInfo SSID:/ {print; exit}' | tr -d '\r' || true)"
    wifi_ssid="$(printf '%s\n' "$wifi_line" | awk -F'SSID: ' 'NF > 1 {split($2, parts, ","); gsub(/"/, "", parts[1]); print parts[1]; exit}')"
    wifi_ssid_bssid="$(printf '%s\n' "$wifi_line" | sed -n 's/.*BSSID: \([^, ]*\).*/\1/p')"
    wifi_rssi="$(printf '%s\n' "$wifi_line" | sed -n 's/.*RSSI: \(-*[0-9][0-9]*\).*/\1/p')"
    wifi_link_speed="$(printf '%s\n' "$wifi_line" | sed -n 's/.*Link speed: \([0-9][0-9]*\)Mbps.*/\1/p')"
    server_ready=0
    if ssh -o BatchMode=yes -o ConnectTimeout=5 "${SERVER_SSH:-sgp-server}" "test -d '${SERVER_REPO:-/root/Starlink_Measure}'" >/dev/null 2>&1; then server_ready=1; fi
    port_listener="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "${SERVER_SSH:-sgp-server}" "ss -H -lun | awk '\$5 ~ /:${UDP_PORT}\$/ {print}'" 2>/dev/null || true)"
    expected_ssid_ok=1
    if [[ -n "$EXPECTED_WIFI_SSID" && "$wifi_ssid" != "$EXPECTED_WIFI_SSID" ]]; then expected_ssid_ok=0; fi
    if (( ICMP_ENABLED )); then icmp_boot="$(adb -s "$ICMP_DEVICE" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"; else icmp_boot=1; fi
    if [[ "$adb_state" == "device" ]] && [[ "$udp_abi" == *arm* ]] && [[ "$udp_boot" == "1" ]] && \
       { (( ICMP_ENABLED == 0 )) || awk -v d="$ICMP_DEVICE" '$1==d && $2=="device" {ok=1} END {exit !ok}' "$snapshot"; } && \
       [[ "$icmp_boot" == "1" ]] && [[ "$expected_ssid_ok" -eq 1 ]] && [[ "$server_ready" -eq 1 ]] && [[ -z "$port_listener" ]]; then
      preflight_file="preflight_attempt_$(printf '%02d' "$((attempt + 1))").txt"
      write_preflight_snapshot "$preflight_file" "pass" "preflight checks passed"
      rm -f "$snapshot"
      break
    fi
    rm -f "$snapshot"
    if (( $(date +%s) - preflight_start >= PREFLIGHT_WAIT_S )); then
      preflight_file="preflight_failed_$(date -u +%Y%m%dT%H%M%SZ).txt"
      preflight_reason="ADB/boot/ABI/SSH/port/SSID preflight did not pass"
      write_preflight_snapshot "$preflight_file" "failed" "$preflight_reason"
      run_dir=""; start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; end_utc="$start_utc"; rc=2
      duration_mode=""; scheduled=""; attempted=""; sent=""; failed=""; elapsed=""; actual=""; received=""; arrivals=""; delays=""; times=""; empty=""; span=""; clocks=""; status="preflight_failed"; reason="$preflight_reason"; icmp_status="skipped"
      append_status_row
      echo "ERROR: preflight did not recover within ${PREFLIGHT_WAIT_S}s" >&2
      exit 1
    fi
    if (( $(date +%s) - PREFLIGHT_LAST_LOG >= 30 )); then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] waiting for preflight: ssid=${wifi_ssid:-unknown} expected=${EXPECTED_WIFI_SSID:-any} server_ready=$server_ready port_listener=${port_listener:-none}" >&2
      PREFLIGHT_LAST_LOG="$(date +%s)"
    fi
    sleep "$PREFLIGHT_POLL_S"
  done

  attempt=$((attempt + 1))
  start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  before="$(find "$SESSION_DIR" -maxdepth 1 -type d -name 'dual_udp_latency_downlink_*' -print | sort | tail -1)"
  child_args=("$SERVER" "$DURATION" "$RATE" "$UDP_PORT" "$UDP_PAYLOAD_BYTES" "${PING_INTERVAL:-0.2}")
  if (( ICMP_ENABLED == 0 )); then child_args=(--no-icmp "${child_args[@]}"); fi
  set +e
  TRACE_LOG_ROOT="$SESSION_DIR" UDP_POSTPROCESS="$POSTPROCESS" UDP_WINDOW_MS="$WINDOW_MS" \
  ADB_UDP_DEVICE="$UDP_DEVICE" ADB_ICMP_DEVICE="$ICMP_DEVICE" \
  SERVER_SSH="${SERVER_SSH:-sgp-server}" SERVER_REPO="${SERVER_REPO:-/root/Starlink_Measure}" \
    bash "$SCRIPT_DIR/run_custom_udp_latency_downlink.sh" "${child_args[@]}"
  rc=$?
  set -e
  end_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  run_dir="$(find "$SESSION_DIR" -maxdepth 1 -type d -name 'dual_udp_latency_downlink_*' -print | sort | tail -1)"
  if [[ -z "$run_dir" || "$run_dir" == "$before" ]]; then run_dir=""; fi
  duration_mode=""; scheduled=""; attempted=""; sent=""; failed=""; elapsed=""; actual=""; received=""; arrivals=""; delays=""; times=""; empty=""; span=""; clocks=""; icmp_status="skipped"
  if [[ -n "$run_dir" && -f "$run_dir/exit_status.txt" ]]; then
    read_status() { awk -F= -v key="$1" '$1==key {print $2; exit}' "$run_dir/exit_status.txt"; }
    duration_mode="$(read_status duration_mode)"; scheduled="$(read_status scheduled_packets)"; attempted="$(read_status attempted_packets)"; sent="$(read_status sent_packets)"; failed="$(read_status send_failed_packets)"
    elapsed="$(read_status sender_elapsed_s)"; actual="$(read_status actual_payload_mbps)"; received="$(read_status received_packets)"; arrivals="$(read_status arrival_rows)"; delays="$(read_status delay_rows)"; times="$(read_status time_rows)"
    empty="$(read_status delay_empty_windows)"; span="$(read_status recv_span_ms)"; clocks="$(read_status clock_sync_samples)"; icmp_status="$(read_status icmp_status)"
  fi
  if [[ "$rc" -eq 0 ]]; then
    success_count=$((success_count + 1)); status=complete; reason="validated compact $POSTPROCESS trace"
  else
    status=failed; reason="runner exit $rc"
  fi
  append_status_row
  if [[ "$success_count" -lt "$SUCCESS_TARGET" && "$COOLDOWN_S" -gt 0 ]]; then sleep "$COOLDOWN_S"; fi
done

session_kv() {
  local file="$1"
  local key="$2"
  awk -F= -v key="$key" '$1 == key {print substr($0, index($0, "=") + 1); exit}' "$file" 2>/dev/null || true
}

archive_attempts() {
  local archive_dir="$SESSION_DIR/failed_attempts"
  local incomplete_dir="$SESSION_DIR/incomplete_attempts"
  local line attempt_no success_no run_id start end exit_code duration_mode scheduled attempted sent failed elapsed actual received arrivals delays times empty span clocks row_status row_reason row_icmp row_icmp_status wifi_ssid wifi_bssid wifi_rssi wifi_link_speed preflight_file
  local source target

  mkdir -p "$archive_dir" "$incomplete_dir"

  while IFS= read -r line; do
    # Bash treats tab as whitespace in IFS and collapses adjacent delimiters,
    # which shifts columns when a failed attempt has empty measurement fields.
    line="${line//$'\t'/$'\x1f'}"
    IFS=$'\x1f' read -r attempt_no success_no run_id start end exit_code duration_mode scheduled attempted sent failed elapsed actual received arrivals delays times empty span clocks row_status row_reason row_icmp row_icmp_status wifi_ssid wifi_bssid wifi_rssi wifi_link_speed preflight_file <<< "$line"
    [[ "$row_status" == "failed" ]] || continue
    [[ "$run_id" == dual_udp_latency_downlink_* && "$run_id" != */* ]] || continue
    source="$SESSION_DIR/$run_id"
    [[ -d "$source" ]] || continue
    mkdir -p "$archive_dir"
    target="$archive_dir/$run_id"
    [[ ! -e "$target" ]] || { echo "ERROR: archive target already exists: $target" >&2; return 1; }
    mv "$source" "$target"
  done < <(tail -n +2 "$SESSION_DIR/repeat_status.tsv")

  shopt -s nullglob
  for source in "$SESSION_DIR"/dual_udp_latency_downlink_*; do
    [[ -d "$source" ]] || continue
    run_id="$(basename "$source")"
    if ! awk -F'\t' -v id="$run_id" 'NR > 1 && $3 == id {found=1} END {exit !found}' "$SESSION_DIR/repeat_status.tsv"; then
      mkdir -p "$incomplete_dir"
      target="$incomplete_dir/$run_id"
      [[ ! -e "$target" ]] || { echo "ERROR: incomplete archive target already exists: $target" >&2; return 1; }
      mv "$source" "$target"
    fi
  done
  shopt -u nullglob
}

write_session_reports() {
  local final_status="$1"
  local line attempt_no success_no run_id start end exit_code duration_mode scheduled attempted sent failed elapsed actual received arrivals delays times empty span clocks row_status row_reason row_icmp row_icmp_status wifi_ssid wifi_bssid wifi_rssi wifi_link_speed preflight_file
  local run_path report_path sender_elapsed actual_mbps receive_mbps socket_drops backpressure lag_p95 lag_max payload_min payload_max receive_api done_received

  printf 'attempt\tsuccess_index\trun_id\trun_path\tstart_utc\tend_utc\tstatus\treason\tsender_offered_mbps\treceiver_mbps\tscheduled_packets\tsent_packets\treceived_packets\tsend_failed_packets\tsocket_rxq_overflow_packets\tcapture_backpressure_records\tapp_read_lag_ms_p95\tapp_read_lag_ms_max\tpayload_bytes_min\tpayload_bytes_max\treceive_api_effective\tdone_received\ticmp_status\twifi_ssid\twifi_rssi\tpreflight_file\n' > "$SESSION_DIR/session_report.tsv"
  while IFS= read -r line; do
    line="${line//$'\t'/$'\x1f'}"
    IFS=$'\x1f' read -r attempt_no success_no run_id start end exit_code duration_mode scheduled attempted sent failed elapsed actual received arrivals delays times empty span clocks row_status row_reason row_icmp row_icmp_status wifi_ssid wifi_bssid wifi_rssi wifi_link_speed preflight_file <<< "$line"
    [[ -n "$attempt_no" ]] || continue
    run_path="$run_id"
    if [[ -d "$SESSION_DIR/$run_id" ]]; then
      report_path="$SESSION_DIR/$run_id"
    elif [[ -d "$SESSION_DIR/failed_attempts/$run_id" ]]; then
      run_path="failed_attempts/$run_id"
      report_path="$SESSION_DIR/$run_path"
    else
      report_path=""
    fi
    sender_elapsed="$(session_kv "$report_path/exit_status.txt" sender_elapsed_s)"
    actual_mbps="$(session_kv "$report_path/exit_status.txt" actual_payload_mbps)"
    receive_mbps="$(session_kv "$report_path/exit_status.txt" average_bandwidth_mbps)"
    socket_drops="$(session_kv "$report_path/exit_status.txt" socket_rxq_overflow_packets)"
    backpressure="$(session_kv "$report_path/exit_status.txt" capture_backpressure_records)"
    lag_p95="$(session_kv "$report_path/udp_phone/downlink_udp_receiver_summary.txt" app_read_lag_ms_p95)"
    lag_max="$(session_kv "$report_path/udp_phone/downlink_udp_receiver_summary.txt" app_read_lag_ms_max)"
    payload_min="$(session_kv "$report_path/udp_phone/downlink_udp_receiver_summary.txt" payload_bytes_min)"
    payload_max="$(session_kv "$report_path/udp_phone/downlink_udp_receiver_summary.txt" payload_bytes_max)"
    receive_api="$(session_kv "$report_path/exit_status.txt" receive_api_effective)"
    done_received="$(session_kv "$report_path/udp_phone/downlink_udp_receiver_summary.txt" done_received)"
    printf '%s\t' "$attempt_no" "$success_no" "$run_id" "$run_path" "$start" "$end" "$row_status" "$row_reason" "$actual_mbps" "$receive_mbps" "$scheduled" "$sent" "$received" "$failed" "$socket_drops" "$backpressure" "$lag_p95" "$lag_max" "$payload_min" "$payload_max" "$receive_api" "$done_received" "$row_icmp_status" "$wifi_ssid" "$wifi_rssi" >> "$SESSION_DIR/session_report.tsv"
    printf '%s\n' "$preflight_file" >> "$SESSION_DIR/session_report.tsv"
  done < <(tail -n +2 "$SESSION_DIR/repeat_status.tsv")

  printf 'classification\trun_id\tgap_rows\n' > "$SESSION_DIR/gap_audit_summary.tsv"
  while IFS= read -r -d '' audit; do
    run_id="$(basename "$(dirname "$audit")")"
    awk -F'\t' -v run="$run_id" 'NR > 1 && $NF != "" {counts[$NF]++} END {for (classification in counts) print classification "\t" run "\t" counts[classification]}' "$audit" >> "$SESSION_DIR/gap_audit_summary.tsv"
  done < <(find "$SESSION_DIR" -type f -name udp_receive_gap_audit.tsv -print0)
  { head -n 1 "$SESSION_DIR/gap_audit_summary.tsv"; tail -n +2 "$SESSION_DIR/gap_audit_summary.tsv" | sort; } > "$SESSION_DIR/gap_audit_summary.tsv.tmp"
  mv "$SESSION_DIR/gap_audit_summary.tsv.tmp" "$SESSION_DIR/gap_audit_summary.tsv"

  {
    printf 'status=%s\n' "$final_status"
    printf 'success_count=%s\n' "$success_count"
    printf 'attempt_count=%s\n' "$attempt"
    printf 'success_target=%s\n' "$SUCCESS_TARGET"
    printf 'max_attempts=%s\n' "$MAX_ATTEMPTS"
    printf 'expected_wifi_ssid=%s\n' "$EXPECTED_WIFI_SSID"
    printf 'completed_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'repeat_status_file=repeat_status.tsv\n'
    printf 'session_report_file=session_report.tsv\n'
    printf 'gap_audit_summary_file=gap_audit_summary.tsv\n'
  } > "$SESSION_DIR/session_summary.txt"
  printf 'final_status=%s\ncompleted_utc=%s\n' "$final_status" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$SESSION_DIR/repeat_metadata.txt"
}

archive_attempts
if [[ "$success_count" -eq "$SUCCESS_TARGET" ]]; then
  if [[ "$attempt" -eq "$SUCCESS_TARGET" ]]; then final_status=complete; else final_status=complete_with_retries; fi
else
  final_status=incomplete
fi
write_session_reports "$final_status"

echo "Processed custom UDP downlink session: $SESSION_DIR"
if [[ "$success_count" -eq "$SUCCESS_TARGET" ]]; then exit 0; fi
echo "ERROR: reached MAX_ATTEMPTS=$MAX_ATTEMPTS with only $success_count/$SUCCESS_TARGET successful runs" >&2
exit 1
