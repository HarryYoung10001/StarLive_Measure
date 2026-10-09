#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage: SERVER_SSH=sgp-server ADB_UDP_DEVICE=<serial> bash traces/scripts/_archived/run_iperf_uplink_rate_compare.sh --no-icmp [server-ip] [duration_s]' \
    'Defaults: rates=10M 20M 30M 40M 50M, duration=30, port=5201, payload=1472, interval=0.1, cooldown=30.'
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
(( ${#POSITIONAL[@]} <= 2 )) || { echo "ERROR: too many positional arguments" >&2; exit 2; }
(( ICMP_ENABLED == 0 )) || { echo "ERROR: comparison runner requires --no-icmp" >&2; exit 2; }

SERVER="${SERVER_IP:-${SERVER:-}}"
DURATION=30
if (( ${#POSITIONAL[@]} == 2 )); then
  SERVER="${POSITIONAL[0]}"
  DURATION="${POSITIONAL[1]}"
elif (( ${#POSITIONAL[@]} == 1 )); then
  if [[ "${POSITIONAL[0]}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    DURATION="${POSITIONAL[0]}"
  else
    SERVER="${POSITIONAL[0]}"
  fi
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TRACE_SCRIPT_DIR="$REPO_DIR/traces/scripts"
SERVER_SSH="${SERVER_SSH:-sgp-server}"
SERVER_REPO="${SERVER_REPO:-/root/Starlink_Measure}"
UDP_DEVICE="${ADB_UDP_DEVICE:-}"
PORT="${UDP_PORT:-5201}"
PAYLOAD_BYTES="${UDP_PAYLOAD_BYTES:-1472}"
INTERVAL="${IPERF_INTERVAL:-0.1}"
COOLDOWN_S="${COOLDOWN_S:-30}"
EXPECTED_WIFI_SSID="${EXPECTED_WIFI_SSID:-STARLINK}"
EXPECTED_DEVICE_MODEL="${EXPECTED_DEVICE_MODEL:-RMX1941}"
PREFLIGHT_WAIT_S="${PREFLIGHT_WAIT_S:-900}"
PREFLIGHT_POLL_S="${PREFLIGHT_POLL_S:-5}"
IPERF_BIN="${IPERF_BIN:-/data/local/tmp/iperf3}"
CUSTOM_SWEEP_DIR="${CUSTOM_SWEEP_DIR:-$REPO_DIR/traces/logs/starlink_uplink_rate_sweep_30s_20260817T062449Z}"
DRY_RUN="${DRY_RUN:-0}"

if [[ -n "${UDP_RATES:-}" ]]; then
  read -r -a RATES <<< "$UDP_RATES"
else
  RATES=(10M 20M 30M 40M 50M)
fi

fail() { echo "ERROR: $*" >&2; exit 2; }

if [[ -z "$SERVER" ]]; then
  SERVER="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" \
    "ip -4 route get 8.8.8.8 | awk '{for (i=1; i<=NF; i++) if (\$i==\"src\") {print \$(i+1); exit}}'" \
    2>/dev/null | tr -d '\r' | awk 'NF {print; exit}')"
fi
[[ -n "$SERVER" ]] || fail "server IP/hostname is required or could not be resolved"
[[ "$SERVER" =~ ^[A-Za-z0-9._:-]+$ ]] || fail "SERVER contains unsupported characters"
[[ "$DURATION" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "duration_s must be numeric"
awk -v value="$DURATION" 'BEGIN {exit !(value > 0)}' || fail "duration_s must be positive"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || fail "UDP_PORT must be in 1..65535"
[[ "$PAYLOAD_BYTES" =~ ^[1-9][0-9]*$ ]] && (( PAYLOAD_BYTES >= 48 )) || fail "UDP_PAYLOAD_BYTES must be >= 48"
[[ "$INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "IPERF_INTERVAL must be numeric"
awk -v value="$INTERVAL" 'BEGIN {exit !(value > 0)}' || fail "IPERF_INTERVAL must be positive"
[[ "$COOLDOWN_S" =~ ^[0-9]+$ ]] || fail "COOLDOWN_S must be non-negative"
[[ "$PREFLIGHT_WAIT_S" =~ ^[1-9][0-9]*$ ]] || fail "PREFLIGHT_WAIT_S must be positive"
[[ "$PREFLIGHT_POLL_S" =~ ^[1-9][0-9]*$ ]] || fail "PREFLIGHT_POLL_S must be positive"
[[ "$DRY_RUN" == 0 || "$DRY_RUN" == 1 ]] || fail "DRY_RUN must be 0 or 1"
[[ -n "$UDP_DEVICE" ]] || fail "ADB_UDP_DEVICE is required"
(( ${#RATES[@]} == 5 )) || fail "UDP_RATES must contain exactly five rates"
for rate in "${RATES[@]}"; do
  [[ "$rate" =~ ^[0-9]+([.][0-9]+)?([KMG]([bB][pP][sS])?|pps)?$ ]] || fail "invalid rate: $rate"
done

SESSION_NAME="${TRACE_SESSION_NAME:-starlink_uplink_iperf_compare_30s_$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$SESSION_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || fail "invalid TRACE_SESSION_NAME"
SESSION_DIR="${TRACE_LOG_ROOT:-$REPO_DIR/traces/logs/$SESSION_NAME}"
if [[ -e "$SESSION_DIR" ]]; then
  [[ -d "$SESSION_DIR" ]] || fail "TRACE_LOG_ROOT is not a directory: $SESSION_DIR"
  [[ -z "$(find "$SESSION_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] || fail "TRACE_LOG_ROOT must be empty: $SESSION_DIR"
fi
mkdir -p "$SESSION_DIR"

printf 'session_name=%s\nserver=%s\nserver_ssh=%s\nserver_repo=%s\nudp_device=%s\nudp_port=%s\nudp_payload_len=%s\nduration_s=%s\niperf_interval_s=%s\ncooldown_s=%s\nrates=%s\nexpected_wifi_ssid=%s\nexpected_device_model=%s\niperf_bin=%s\ncustom_sweep_dir=%s\nicmp_enabled=false\ncreated_utc=%s\n' \
  "$SESSION_NAME" "$SERVER" "$SERVER_SSH" "$SERVER_REPO" "$UDP_DEVICE" "$PORT" "$PAYLOAD_BYTES" "$DURATION" "$INTERVAL" "$COOLDOWN_S" "${RATES[*]}" "$EXPECTED_WIFI_SSID" "$EXPECTED_DEVICE_MODEL" "$IPERF_BIN" "$CUSTOM_SWEEP_DIR" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$SESSION_DIR/compare_metadata.txt"
printf 'rate\tstart_utc\tend_utc\tstatus\tclient_exit_code\tserver_exit_code\tnetwork_changed\ttrace_path\n' > "$SESSION_DIR/compare_status.tsv"
printf 'rate\tstatus\tclient_elapsed_s\tclient_sent_mbps\tserver_received_mbps_mean\tserver_received_mbps_median\tserver_received_mbps_min\tserver_received_mbps_max\tserver_total_packets\tserver_lost_packets\tserver_loss_percent\tserver_interval_rows\tserver_last_interval_s\tclient_exit_code\tserver_exit_code\tnetwork_changed\ttrace_path\n' > "$SESSION_DIR/compare_summary.tsv"
printf 'rate\tcustom_sender_mbps\tcustom_received_packets\tcustom_arrival_max_ms\tcustom_socket_rxq_overflow_packets\tiperf_sender_mbps\tiperf_receiver_mbps\tiperf_loss_percent\tiperf_last_interval_s\tstatus\n' > "$SESSION_DIR/iperf_vs_custom_summary.tsv"

if [[ "$DRY_RUN" == 1 ]]; then
  for rate in "${RATES[@]}"; do
    safe_rate="$(printf '%s' "$rate" | tr -c 'A-Za-z0-9._-' '_')"
    mkdir -p "$SESSION_DIR/rate_$safe_rate"
    printf 'rate=%s\nduration_s=%s\nudp_payload_len=%s\niperf_interval_s=%s\nicmp_enabled=false\ndry_run=true\n' "$rate" "$DURATION" "$PAYLOAD_BYTES" "$INTERVAL" > "$SESSION_DIR/rate_$safe_rate/metadata.txt"
    printf '%s\tdry_run\t\t\t\t\t\t\t\t\t\t\t\t\t\t\tfalse\trate_%s\n' "$rate" "$safe_rate" >> "$SESSION_DIR/compare_summary.tsv"
    printf '%s\tdry_run\t\t\t\t\t\t\t\t\n' "$rate" >> "$SESSION_DIR/iperf_vs_custom_summary.tsv"
  done
  printf 'status=dry_run\nrates=%s\n' "${RATES[*]}" > "$SESSION_DIR/session_summary.txt"
  exit 0
fi

for command in adb ssh scp python3 awk; do
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
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$status" "$reason" "$UDP_DEVICE" "${UDP_MODEL:-}" "${UDP_ABI_LIST:-}" "${UDP_BOOT_COMPLETED:-}" "$WIFI_SSID" "$WIFI_BSSID" "$WIFI_RSSI" "$WIFI_LINK_SPEED" "$EXPECTED_WIFI_SSID" "$EXPECTED_DEVICE_MODEL" "$SERVER_SSH" "${SERVER_PLATFORM:-}" "${SERVER_READY:-0}" "$PORT" "${PORT_LISTENER:-none}" > "$path"
}

preflight() {
  local start now snapshot
  start="$(date +%s)"
  while true; do
    snapshot="$(mktemp "${TMPDIR:-/tmp}/iperf_compare_adb.XXXXXX")"
    adb devices > "$snapshot" 2>&1 || true
    UDP_ABI_LIST="$(adb -s "$UDP_DEVICE" shell getprop ro.product.cpu.abilist 2>/dev/null | tr -d '\r' || true)"
    UDP_BOOT_COMPLETED="$(adb -s "$UDP_DEVICE" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
    UDP_MODEL="$(adb -s "$UDP_DEVICE" shell getprop ro.product.model 2>/dev/null | tr -d '\r' || true)"
    read_wifi
    SERVER_READY=0
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "command -v iperf3 >/dev/null" >/dev/null 2>&1 && SERVER_READY=1
    SERVER_PLATFORM="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" 'uname -s -m' 2>/dev/null | tr -d '\r' || true)"
    PORT_LISTENER="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "ss -H -lun | awk '\$5 ~ /:${PORT}\$/ {print}'" 2>/dev/null || true)"
    if awk -v d="$UDP_DEVICE" '$1 == d && $2 == "device" {ok=1} END {exit !ok}' "$snapshot" && \
       [[ "$UDP_ABI_LIST" == *arm* ]] && [[ "$UDP_BOOT_COMPLETED" == 1 ]] && \
       [[ "$UDP_MODEL" == "$EXPECTED_DEVICE_MODEL" ]] && [[ "$WIFI_SSID" == "$EXPECTED_WIFI_SSID" ]] && \
       [[ "$SERVER_PLATFORM" == "Linux x86_64" ]] && [[ "$SERVER_READY" == 1 ]] && [[ -z "$PORT_LISTENER" ]]; then
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

read_metric() {
  local file="$1" key="$2"
  awk -F= -v key="$key" '$1 == key {print $2; exit}' "$file" 2>/dev/null || true
}

tsv_value() {
  local file="$1" rate="$2" column="$3"
  [[ -s "$file" ]] || return 0
  awk -F '\t' -v rate="$rate" -v column="$column" 'NR == 1 {for (i=1; i<=NF; i++) header[$i]=i; next} $1 == rate {print $(header[column]); exit}' "$file"
}

wait_for_server_ready() {
  local deadline now
  deadline=$(( $(date +%s) + 30 ))
  while true; do
    # iperf3 opens its TCP control listener before the UDP data socket.
    PORT_LISTENER="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "ss -H -ltn | awk '\$4 ~ /:${PORT}\$/ {print; exit}'" 2>/dev/null || true)"
    [[ -n "$PORT_LISTENER" ]] && return 0
    now="$(date +%s)"
    (( now < deadline )) || return 1
    sleep 1
  done
}

wait_for_server_exit() {
  local remote_dir="$1" deadline now value
  deadline=$(( $(date +%s) + 60 ))
  while true; do
    value="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "cat '$remote_dir/server_exit_code.txt' 2>/dev/null || true" | tr -d '\r')"
    [[ "$value" =~ ^[0-9]+$ ]] && { printf '%s' "$value"; return 0; }
    now="$(date +%s)"
    (( now < deadline )) || return 1
    sleep 1
  done
}

cleanup_server_pid() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "kill '$pid' 2>/dev/null || true" >/dev/null 2>&1 || true
}

normalize_client_json() {
  local raw="$1" normalized="$2"
  python3 - "$raw" "$normalized" <<'PY'
import json
import sys
from pathlib import Path

raw = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
start = raw.find("{")
if start < 0:
    raise SystemExit("client output contains no JSON object")
value, _ = json.JSONDecoder().raw_decode(raw[start:])
Path(sys.argv[2]).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
PY
}

active_server_pid=""
cleanup_on_exit() {
  cleanup_server_pid "$active_server_pid"
}
trap cleanup_on_exit EXIT

rate_index=0
failed_rates=0
preflight_failed=false
for rate in "${RATES[@]}"; do
  rate_index=$((rate_index + 1))
  safe_rate="$(printf '%s' "$rate" | tr -c 'A-Za-z0-9._-' '_')"
  run_dir="$SESSION_DIR/rate_$safe_rate"
  mkdir -p "$run_dir"
  preflight_file="$SESSION_DIR/preflight_rate_$(printf '%02d' "$rate_index").txt"
  postflight_file="$SESSION_DIR/postflight_rate_$(printf '%02d' "$rate_index").txt"
  start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if ! preflight; then
    write_preflight "$preflight_file" failed "${PREFLIGHT_REASON:-preflight timeout}"
    printf '%s\t%s\t%s\tpreflight_failed\t1\t\tfalse\trate_%s\n' "$rate" "$start_utc" "$start_utc" "$safe_rate" >> "$SESSION_DIR/compare_status.tsv"
    printf '%s\tpreflight_failed\t\t\t\t\t\t\t\t\t\t\t\t1\t\tfalse\trate_%s\n' "$rate" "$safe_rate" >> "$SESSION_DIR/compare_summary.tsv"
    failed_rates=$((failed_rates + 1))
    preflight_failed=true
    break
  fi

  write_preflight "$preflight_file" pass ready
  preflight_bssid="$WIFI_BSSID"
  remote_dir="$SERVER_REPO/traces/logs/$SESSION_NAME/rate_$safe_rate"
  remote_pid=""
  client_rc=1
  server_rc=1
  parser_rc=2
  network_changed=false
  printf 'rate=%s\nserver=%s\nserver_ssh=%s\nserver_remote_dir=%s\nduration_s=%s\nudp_port=%s\nudp_payload_len=%s\niperf_interval_s=%s\nadb_device=%s\nexpected_wifi_ssid=%s\nexpected_device_model=%s\nicmp_enabled=false\n' \
    "$rate" "$SERVER" "$SERVER_SSH" "$remote_dir" "$DURATION" "$PORT" "$PAYLOAD_BYTES" "$INTERVAL" "$UDP_DEVICE" "$EXPECTED_WIFI_SSID" "$EXPECTED_DEVICE_MODEL" > "$run_dir/metadata.txt"

  ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" \
    "mkdir -p '$remote_dir'; rm -f '$remote_dir/iperf3_server.json' '$remote_dir/iperf3_server.stderr' '$remote_dir/server_exit_code.txt'; nohup sh -c 'iperf3 -s -p $PORT -i $INTERVAL -1 -J --forceflush > \"$remote_dir/iperf3_server.json\" 2> \"$remote_dir/iperf3_server.stderr\"; rc=\$?; printf \"%s\\n\" \"\$rc\" > \"$remote_dir/server_exit_code.txt\"' </dev/null >/dev/null 2>&1 & printf '%s\\n' \"\$!\"" \
    > "$run_dir/server_start.stdout" 2> "$run_dir/server_start.stderr" || true
  remote_pid="$(tr -d '[:space:]' < "$run_dir/server_start.stdout" 2>/dev/null || true)"
  active_server_pid="$remote_pid"

  if wait_for_server_ready; then
    if adb -s "$UDP_DEVICE" shell "$IPERF_BIN -c $SERVER -p $PORT -u -b $rate -l $PAYLOAD_BYTES -t $DURATION -i $INTERVAL -J --get-server-output" \
      > "$run_dir/iperf3_client.stdout" 2> "$run_dir/iperf3_client.stderr"; then
      client_rc=0
    else
      client_rc=$?
    fi
    normalize_client_json "$run_dir/iperf3_client.stdout" "$run_dir/iperf3_client.json" || client_rc=1
  else
    printf 'server_ready_timeout\n' > "$run_dir/iperf3_client.stderr"
  fi
  printf '%s\n' "$client_rc" > "$run_dir/client_exit_code.txt"

  if server_rc_value="$(wait_for_server_exit "$remote_dir")"; then
    server_rc="$server_rc_value"
    active_server_pid=""
  else
    cleanup_server_pid "$remote_pid"
    active_server_pid=""
    server_rc=124
  fi
  scp -q "$SERVER_SSH:$remote_dir/iperf3_server.json" "$run_dir/iperf3_server.json" > "$run_dir/server_pull.stdout" 2> "$run_dir/server_pull.stderr" || true
  scp -q "$SERVER_SSH:$remote_dir/iperf3_server.stderr" "$run_dir/iperf3_server.stderr" >> "$run_dir/server_pull.stdout" 2>> "$run_dir/server_pull.stderr" || true
  printf '%s\n' "$server_rc" > "$run_dir/server_exit_code.txt"

  parser_rc=0
  if python3 "$TRACE_SCRIPT_DIR/IperfUplinkCompare.py" \
    --client-json "$run_dir/iperf3_client.json" \
    --server-json "$run_dir/iperf3_server.json" \
    --intervals-output "$run_dir/iperf_intervals.tsv" \
    --metrics-output "$run_dir/iperf_metrics.txt" > "$run_dir/parser.stdout" 2> "$run_dir/parser.stderr"; then
    :
  else
    parser_rc=$?
  fi
  parser_status="$(read_metric "$run_dir/iperf_metrics.txt" status)"

  read_wifi
  write_preflight "$postflight_file" pass postflight
  if [[ "$UDP_MODEL" != "$EXPECTED_DEVICE_MODEL" || "$WIFI_SSID" != "$EXPECTED_WIFI_SSID" || "$WIFI_BSSID" != "$preflight_bssid" ]]; then
    network_changed=true
  fi
  if [[ "$network_changed" == true ]]; then
    status=network_changed
  elif [[ "$client_rc" -ne 0 || "$server_rc" -ne 0 || "$parser_rc" -eq 2 ]]; then
    status=iperf_failed
  elif [[ "$parser_status" != complete ]]; then
    status=receiver_incomplete
  else
    status=complete
  fi
  [[ "$status" == complete ]] || failed_rates=$((failed_rates + 1))

  client_elapsed="$(read_metric "$run_dir/iperf_metrics.txt" client_elapsed_s)"
  client_sent="$(read_metric "$run_dir/iperf_metrics.txt" client_sent_mbps)"
  receiver_mean="$(read_metric "$run_dir/iperf_metrics.txt" server_received_mbps_mean)"
  receiver_median="$(read_metric "$run_dir/iperf_metrics.txt" server_received_mbps_median)"
  receiver_min="$(read_metric "$run_dir/iperf_metrics.txt" server_received_mbps_min)"
  receiver_max="$(read_metric "$run_dir/iperf_metrics.txt" server_received_mbps_max)"
  total_packets="$(read_metric "$run_dir/iperf_metrics.txt" server_total_packets)"
  lost_packets="$(read_metric "$run_dir/iperf_metrics.txt" server_lost_packets)"
  loss_percent="$(read_metric "$run_dir/iperf_metrics.txt" server_loss_percent)"
  interval_rows="$(read_metric "$run_dir/iperf_metrics.txt" server_interval_rows)"
  last_interval="$(read_metric "$run_dir/iperf_metrics.txt" server_last_interval_s)"
  trace_path="rate_$safe_rate"
  end_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$rate" "$start_utc" "$end_utc" "$status" "$client_rc" "$server_rc" "$network_changed" "$trace_path" >> "$SESSION_DIR/compare_status.tsv"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$rate" "$status" "$client_elapsed" "$client_sent" "$receiver_mean" "$receiver_median" "$receiver_min" "$receiver_max" "$total_packets" "$lost_packets" "$loss_percent" "$interval_rows" "$last_interval" "$client_rc" "$server_rc" "$network_changed" "$trace_path" >> "$SESSION_DIR/compare_summary.tsv"

  custom_summary="$CUSTOM_SWEEP_DIR/sweep_summary.tsv"
  custom_sender="$(tsv_value "$custom_summary" "$rate" actual_payload_mbps)"
  custom_received="$(tsv_value "$custom_summary" "$rate" received_packets)"
  custom_arrival="$(tsv_value "$custom_summary" "$rate" arrival_max_ms)"
  custom_drops="$(tsv_value "$custom_summary" "$rate" socket_rxq_overflow_packets)"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$rate" "$custom_sender" "$custom_received" "$custom_arrival" "$custom_drops" "$client_sent" "$receiver_mean" "$loss_percent" "$last_interval" "$status" >> "$SESSION_DIR/iperf_vs_custom_summary.tsv"
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_SSH" "rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true
  [[ "$rate_index" -lt "${#RATES[@]}" ]] && sleep "$COOLDOWN_S"
done

if [[ "$preflight_failed" == true ]]; then
  final_status=preflight_failed
elif (( failed_rates == 0 )); then
  final_status=complete
else
  final_status=completed_with_failures
fi
{
  printf 'status=%s\n' "$final_status"
  printf 'rates_total=%s\nrates_attempted=%s\nrates_failed=%s\n' "${#RATES[@]}" "$rate_index" "$failed_rates"
  printf 'rates=%s\nduration_s=%s\npayload_bytes=%s\ninterval_s=%s\n' "${RATES[*]}" "$DURATION" "$PAYLOAD_BYTES" "$INTERVAL"
  printf 'compare_summary=compare_summary.tsv\niperf_vs_custom_summary=iperf_vs_custom_summary.tsv\n'
} > "$SESSION_DIR/session_summary.txt"
printf 'final_status=%s\nrates_attempted=%s\nrates_failed=%s\n' "$final_status" "$rate_index" "$failed_rates" >> "$SESSION_DIR/compare_metadata.txt"
echo "iperf uplink comparison outputs saved to: $SESSION_DIR"
[[ "$final_status" == complete ]] && exit 0
exit 1
