#!/usr/bin/env bash
set -euo pipefail

# Sequential sweep for the timestamped custom UDP downlink flow.

usage() {
  cat <<'USAGE'
Usage:
  SERVER_SSH=sgp-server \
    bash traces/scripts/_archived/run_custom_udp_latency_downlink_sweep.sh [--no-icmp] [server-ip] [duration_s]
  SERVER_SSH=sgp-server \
    bash traces/scripts/_archived/run_custom_udp_latency_downlink_sweep.sh \
    [--no-icmp] --resume <session-dir> [server-ip] [duration_s]

Defaults:
  rates=200M 210M ... 590M (40 rates)
  duration_s=30, UDP payload=1200 bytes, port=5201, ping interval=0.2s
  ICMP is enabled by default; --no-icmp runs with only the UDP phone.

Environment:
  SERVER_IP          explicit target IP; if unset, resolve from SERVER_SSH route
  TRACE_SESSION_NAME, TRACE_LOG_ROOT, SERVER_SSH, SERVER_REPO
  ADB_UDP_DEVICE (required), ADB_ICMP_DEVICE (required unless --no-icmp)
  UDP_RATES, UDP_PORT
  UDP_PAYLOAD_BYTES, PING_INTERVAL, UDP_TIMEOUT_S, UDP_POSTPROCESS,
  UDP_WINDOW_MS, UDP_DURATION_MODE, DRY_RUN, FORCE_RERUN
USAGE
}

ICMP_ENABLED=1
RESUME_MODE=0
RESUME_SESSION_DIR="${RESUME_SESSION_DIR:-}"
POSITIONAL=()
while (($#)); do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --no-icmp) ICMP_ENABLED=0; shift ;;
    --resume)
      (( RESUME_MODE == 0 )) || { echo "ERROR: --resume specified more than once" >&2; exit 2; }
      [[ $# -ge 2 ]] || { echo "ERROR: --resume requires a session directory" >&2; exit 2; }
      RESUME_MODE=1; RESUME_SESSION_DIR="$2"; shift 2 ;;
    --) shift; POSITIONAL+=("$@"); break ;;
    --*) echo "ERROR: unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
(( ${#POSITIONAL[@]} <= 2 )) || { echo "ERROR: too many positional arguments" >&2; usage >&2; exit 2; }
if [[ -n "$RESUME_SESSION_DIR" ]]; then RESUME_MODE=1; fi

SERVER="${POSITIONAL[0]-${SERVER_IP:-${SERVER:-}}}"
DURATION="${POSITIONAL[1]-30}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SINGLE_RUNNER="$SCRIPT_DIR/run_custom_udp_latency_downlink.sh"
SERVER_SSH="${SERVER_SSH:-sgp-server}"
SERVER_REPO="${SERVER_REPO:-/root/Starlink_Measure}"
UDP_DEVICE="${ADB_UDP_DEVICE:-}"
ICMP_DEVICE="${ADB_ICMP_DEVICE:-}"
PORT="${UDP_PORT:-5201}"
PAYLOAD_BYTES="${UDP_PAYLOAD_BYTES:-1200}"
PING_INTERVAL="${PING_INTERVAL:-0.2}"
UDP_TIMEOUT_S="${UDP_TIMEOUT_S:-5}"
POSTPROCESS="${UDP_POSTPROCESS:-arrival}"
WINDOW_MS="${UDP_WINDOW_MS:-10}"
DURATION_MODE="${UDP_DURATION_MODE:-}"
DRY_RUN="${DRY_RUN:-0}"
FORCE_RERUN="${FORCE_RERUN:-0}"
DEFAULT_RATES=()
if [[ "$RESUME_MODE" -eq 1 ]]; then
  for rate_mbps in $(seq 370 10 590); do DEFAULT_RATES+=("${rate_mbps}M"); done
  for rate_mbps in $(seq 100 10 190); do DEFAULT_RATES+=("${rate_mbps}M"); done
else
  for rate_mbps in $(seq 200 10 590); do DEFAULT_RATES+=("${rate_mbps}M"); done
fi

fail() { echo "ERROR: $*" >&2; exit 2; }

if [[ -z "$SERVER" ]]; then
  command -v ssh >/dev/null 2>&1 || fail "ssh is required to resolve SERVER_IP"
  SERVER="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" \
    "ip -4 route get 8.8.8.8 | awk '{for (i=1; i<=NF; i++) if (\$i==\"src\") {print \$(i+1); exit}}'" \
    2>/dev/null | tr -d '\r' | awk 'NF { print; exit }')"
fi
[[ -n "$SERVER" ]] || { usage >&2; fail "server IP/hostname is required or could not be resolved"; }
if [[ "$DRY_RUN" == "0" ]]; then
  [[ -n "$UDP_DEVICE" ]] || fail "ADB_UDP_DEVICE is required"
  if (( ICMP_ENABLED )); then
    [[ -n "$ICMP_DEVICE" ]] || fail "ADB_ICMP_DEVICE is required unless --no-icmp is used"
  fi
fi
[[ "$SERVER" =~ ^[A-Za-z0-9._:-]+$ ]] || fail "SERVER contains unsupported characters"
[[ "$DURATION" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "duration_s must be numeric"
awk -v d="$DURATION" 'BEGIN { exit !(d > 0) }' || fail "duration_s must be positive"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || fail "UDP_PORT must be in 1..65535"
[[ "$PAYLOAD_BYTES" =~ ^[1-9][0-9]*$ ]] && (( PAYLOAD_BYTES >= 48 )) || fail "UDP_PAYLOAD_BYTES must be >= 48"
[[ "$PING_INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "PING_INTERVAL must be numeric"
awk -v i="$PING_INTERVAL" 'BEGIN { exit !(i > 0) }' || fail "PING_INTERVAL must be positive"
[[ "$DRY_RUN" == "0" || "$DRY_RUN" == "1" ]] || fail "DRY_RUN must be 0 or 1"
[[ "$FORCE_RERUN" == "0" || "$FORCE_RERUN" == "1" ]] || fail "FORCE_RERUN must be 0 or 1"
[[ "$POSTPROCESS" == "arrival" || "$POSTPROCESS" == "latency" ]] || fail "UDP_POSTPROCESS must be arrival or latency"
[[ "$WINDOW_MS" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v w="$WINDOW_MS" 'BEGIN {exit !(w>0)}' || fail "UDP_WINDOW_MS must be positive"
if (( ICMP_ENABLED )); then
  [[ "$UDP_DEVICE" != "$ICMP_DEVICE" ]] || fail "UDP and ICMP devices must differ"
fi

if [[ -n "${UDP_RATES:-}" ]]; then
  read -r -a RATES <<< "$UDP_RATES"
else
  RATES=("${DEFAULT_RATES[@]}")
fi
(( ${#RATES[@]} > 0 )) || fail "at least one UDP rate is required"
for rate in "${RATES[@]}"; do
  [[ "$rate" =~ ^[0-9]+([.][0-9]+)?([KMG]([bB][pP][sS])?|pps)?$ ]] || fail "invalid UDP rate: $rate"
done

SESSION_NAME="${TRACE_SESSION_NAME:-custom_udp_latency_downlink_$(date -u +%Y%m%dT%H%M%SZ)}"
if [[ "$RESUME_MODE" -eq 1 ]]; then
  [[ -n "$RESUME_SESSION_DIR" ]] || fail "--resume requires a session directory"
  [[ -d "$RESUME_SESSION_DIR" ]] || fail "resume session directory does not exist: $RESUME_SESSION_DIR"
  SESSION_DIR="$(cd "$RESUME_SESSION_DIR" && pwd)"
  SESSION_NAME="$(basename "$SESSION_DIR")"
else
  [[ "$SESSION_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || fail "invalid TRACE_SESSION_NAME"
  SESSION_DIR="${TRACE_LOG_ROOT:-$REPO_DIR/traces/logs/$SESSION_NAME}"
  if [[ -e "$SESSION_DIR" ]]; then
    [[ -d "$SESSION_DIR" ]] || fail "TRACE_LOG_ROOT is not a directory: $SESSION_DIR"
    [[ -z "$(find "$SESSION_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] || fail "TRACE_LOG_ROOT must be empty: $SESSION_DIR"
  fi
fi
REQUESTED_ICMP_ENABLED="$([[ "$ICMP_ENABLED" -eq 1 ]] && echo true || echo false)"
if [[ "$RESUME_MODE" -eq 1 ]]; then
  EXISTING_ICMP_ENABLED=true
  if [[ -f "$SESSION_DIR/sweep_metadata.txt" ]]; then
    EXISTING_ICMP_ENABLED="$(awk -F= '$1 == "icmp_enabled" {print $2; exit}' "$SESSION_DIR/sweep_metadata.txt")"
    [[ "$EXISTING_ICMP_ENABLED" == "true" || "$EXISTING_ICMP_ENABLED" == "false" ]] || EXISTING_ICMP_ENABLED=true
  fi
  [[ "$EXISTING_ICMP_ENABLED" == "$REQUESTED_ICMP_ENABLED" ]] || \
    fail "ICMP mode does not match existing session (requested=$REQUESTED_ICMP_ENABLED existing=$EXISTING_ICMP_ENABLED)"
  EXISTING_POSTPROCESS="$(awk -F= '$1=="postprocess" {print $2; exit}' "$SESSION_DIR/sweep_metadata.txt" 2>/dev/null || true)"
  if [[ -n "$EXISTING_POSTPROCESS" ]]; then
    [[ "$EXISTING_POSTPROCESS" == "$POSTPROCESS" ]] || fail "UDP_POSTPROCESS does not match existing session"
  fi
fi
mkdir -p "$SESSION_DIR"

COMPLETE_RATES_FILE="${TMPDIR:-/tmp}/custom_udp_complete_rates.$$.txt"
: > "$COMPLETE_RATES_FILE"
RUN_LOG_ROOT="$SESSION_DIR"
DRY_RUN_ROOT=""
if [[ "$RESUME_MODE" -eq 1 && "$DRY_RUN" -eq 1 ]]; then
  DRY_RUN_ROOT="${TMPDIR:-/tmp}/custom_udp_resume_dry_run.$$"
  mkdir -p "$DRY_RUN_ROOT"
  RUN_LOG_ROOT="$DRY_RUN_ROOT"
fi
cleanup() {
  rm -f "$COMPLETE_RATES_FILE"
  [[ -z "$DRY_RUN_ROOT" ]] || rm -rf "$DRY_RUN_ROOT"
}
trap cleanup EXIT
if [[ "$RESUME_MODE" -eq 1 ]]; then
  [[ -f "$SESSION_DIR/sweep_status.tsv" ]] || fail "resume session is missing sweep_status.tsv"
  if [[ ! -f "$SESSION_DIR/sweep_status_history.tsv" ]]; then
    cp "$SESSION_DIR/sweep_status.tsv" "$SESSION_DIR/sweep_status_history.tsv"
  fi
  if [[ -f "$SESSION_DIR/compact_trace_summary.tsv" ]]; then
    awk -F '\t' 'NR>1 && $4=="complete" {print $1}' "$SESSION_DIR/compact_trace_summary.tsv" >> "$COMPLETE_RATES_FILE"
  elif [[ -f "$SESSION_DIR/trace_summary.csv" ]] && command -v python3 >/dev/null 2>&1; then
    while IFS= read -r rate; do [[ -n "$rate" ]] && printf '%s\n' "$rate" >> "$COMPLETE_RATES_FILE"; done < <(
      python3 -c 'import csv,sys; [print(r["offered_rate"]) for r in csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8")) if r.get("status")=="complete" and r.get("offered_rate")]' "$SESSION_DIR/trace_summary.csv"
    )
  fi
  if [[ "$DRY_RUN" == "0" && -f "$SESSION_DIR/trace_summary.csv" ]]; then
    python3 - "$SESSION_DIR" <<'PY'
import csv
import shutil
import sys
from pathlib import Path

session = Path(sys.argv[1])
archive = session / "failed_attempts"
archive.mkdir(exist_ok=True)
summary = session / "trace_summary.csv"
with summary.open(newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
for row in rows:
    if row.get("status") == "complete":
        continue
    trace_dir = row.get("trace_dir", "")
    source = session / trace_dir
    if not trace_dir or not source.is_dir() or source.is_symlink():
        continue
    target = archive / source.name
    if target.exists():
        target = archive / f"{source.name}_{len(list(archive.glob(source.name + '_*')))+1}"
    shutil.move(str(source), str(target))
PY
  fi
fi

if [[ "$DRY_RUN" == "0" ]]; then
  for command in adb ssh python3 go scp; do
    command -v "$command" >/dev/null 2>&1 || fail "$command is required"
  done
  adb devices > "$SESSION_DIR/adb_devices_preflight.txt" 2>&1 || fail "failed to list ADB devices"
  for device in "$UDP_DEVICE"; do
    awk -v d="$device" '$1 == d && $2 == "device" { found=1 } END { exit !found }' \
      "$SESSION_DIR/adb_devices_preflight.txt" || fail "ADB device is not ready: $device"
  done
  if (( ICMP_ENABLED )); then
    awk -v d="$ICMP_DEVICE" '$1 == d && $2 == "device" { found=1 } END { exit !found }' \
      "$SESSION_DIR/adb_devices_preflight.txt" || fail "ADB device is not ready: $ICMP_DEVICE"
  fi
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" \
    "command -v python3 >/dev/null && test -f '$SERVER_REPO/server_receiver/scripts/downlink_udp_sender.py'" \
    || fail "remote Python sender or repository is unavailable"
  sample_clock() {
    local label="$1"
    shift
    local before after value
    before="$(date +%s)"
    value="$("$@" 2>/dev/null | tr -d '\r' | awk 'NF { print; exit }')"
    after="$(date +%s)"
    printf '%s_before_s=%s\n%s_epoch_s=%s\n%s_after_s=%s\n' \
      "$label" "$before" "$label" "$value" "$label" "$after"
  }
  {
    sample_clock server ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" date +%s
    sample_clock udp_phone adb -s "$UDP_DEVICE" shell date +%s
    if (( ICMP_ENABLED )); then sample_clock icmp_phone adb -s "$ICMP_DEVICE" shell date +%s; fi
  } > "$SESSION_DIR/clock_preflight.txt"
  python3 - "$SESSION_DIR/clock_preflight.txt" <<'PY'
import sys

values = {}
for line in open(sys.argv[1], encoding="utf-8"):
    key, _, value = line.strip().partition("=")
    if key and value.isdigit():
        values[key] = int(value)
offsets = {}
for label in ("server", "udp_phone", "icmp_phone"):
    before = values.get(f"{label}_before_s")
    epoch = values.get(f"{label}_epoch_s")
    after = values.get(f"{label}_after_s")
    if before is not None and epoch is not None and after is not None:
        # The remote timestamp is emitted immediately before the command
        # returns; compare against local after-time to avoid SSH startup skew.
        offsets[label] = epoch - after
if len(offsets) >= 2 and max(offsets.values()) - min(offsets.values()) > 2:
    print(f"WARNING: clock preflight spread exceeds 2s: {offsets}", file=sys.stderr)
PY
  PORT_LISTENER="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER_SSH" \
    "ss -H -lun | awk '\$5 ~ /:$PORT\$/ { print; exit }'" 2>/dev/null || true)"
  [[ -z "$PORT_LISTENER" ]] || fail "UDP port $PORT is already in use: $PORT_LISTENER"
fi

if [[ "$RESUME_MODE" -eq 1 ]]; then
  if [[ "$DRY_RUN" == "0" ]]; then
    cat >> "$SESSION_DIR/sweep_metadata.txt" <<META
resume_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
resume_server=$SERVER
resume_udp_device=$UDP_DEVICE
resume_icmp_enabled=$REQUESTED_ICMP_ENABLED
resume_icmp_device=$([[ "$ICMP_ENABLED" -eq 1 ]] && echo "$ICMP_DEVICE" || echo disabled)
resume_rates=${RATES[*]}
META
  fi
else
  cat > "$SESSION_DIR/sweep_metadata.txt" <<META
session_name=$SESSION_NAME
server=$SERVER
server_ssh=$SERVER_SSH
server_repo=$SERVER_REPO
udp_direction=downlink
flow_type=custom_timestamped_udp
postprocess=$POSTPROCESS
window_ms=$WINDOW_MS
duration_mode=${DURATION_MODE:-automatic}
duration_s=$DURATION
udp_port=$PORT
udp_payload_len=$PAYLOAD_BYTES
ping_interval_s=$PING_INTERVAL
udp_timeout_s=$UDP_TIMEOUT_S
icmp_enabled=$REQUESTED_ICMP_ENABLED
udp_device=$UDP_DEVICE
icmp_device=$([[ "$ICMP_ENABLED" -eq 1 ]] && echo "$ICMP_DEVICE" || echo disabled)
rates=${RATES[*]}
server_ip_resolved=$SERVER
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
META
  printf 'rate\tstart_utc\tend_utc\texit_code\n' > "$SESSION_DIR/sweep_status.tsv"
fi
if [[ ! -f "$SESSION_DIR/sweep_status.tsv" ]]; then
  printf 'rate\tstart_utc\tend_utc\texit_code\n' > "$SESSION_DIR/sweep_status.tsv"
fi

update_canonical_status() {
  local rate="$1" start_utc="$2" end_utc="$3" exit_code="$4"
  local tmp_path="$SESSION_DIR/.sweep_status.tsv.tmp.$$"
  awk -F '\t' -v rate="$rate" 'NR == 1 || $1 != rate' \
    "$SESSION_DIR/sweep_status.tsv" > "$tmp_path"
  printf '%s\t%s\t%s\t%s\n' "$rate" "$start_utc" "$end_utc" "$exit_code" >> "$tmp_path"
  mv "$tmp_path" "$SESSION_DIR/sweep_status.tsv"
}

FAILED=0
for rate in "${RATES[@]}"; do
  if [[ "$RESUME_MODE" -eq 1 && "$FORCE_RERUN" -eq 0 && -s "$COMPLETE_RATES_FILE" ]] && grep -Fqx "$rate" "$COMPLETE_RATES_FILE"; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] skipping already complete $rate"
    continue
  fi
  START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[$START_UTC] starting custom UDP latency $rate"
  set +e
  CHILD_ARGS=("$SERVER" "$DURATION" "$rate" "$PORT" "$PAYLOAD_BYTES" "$PING_INTERVAL")
  if (( ICMP_ENABLED == 0 )); then CHILD_ARGS=(--no-icmp "${CHILD_ARGS[@]}"); fi
  TRACE_LOG_ROOT="$RUN_LOG_ROOT" \
  ADB_UDP_DEVICE="$UDP_DEVICE" \
  ADB_ICMP_DEVICE="$ICMP_DEVICE" \
  SERVER_SSH="$SERVER_SSH" \
  SERVER_REPO="$SERVER_REPO" \
  UDP_TIMEOUT_S="$UDP_TIMEOUT_S" \
  UDP_POSTPROCESS="$POSTPROCESS" \
  UDP_WINDOW_MS="$WINDOW_MS" \
  UDP_DURATION_MODE="$DURATION_MODE" \
  DRY_RUN="$DRY_RUN" \
  bash "$SINGLE_RUNNER" "${CHILD_ARGS[@]}"
  RUN_RC=$?
  set -e
  END_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ "$DRY_RUN" == "0" ]]; then
    update_canonical_status "$rate" "$START_UTC" "$END_UTC" "$RUN_RC"
  fi
  if [[ "$RUN_RC" -ne 0 ]]; then
    FAILED=1
    echo "WARNING: $rate failed with exit code $RUN_RC; continuing" >&2
  fi
done

if [[ "$DRY_RUN" == "1" ]]; then
  echo "Dry run complete. Plans saved under: $RUN_LOG_ROOT"
  exit "$FAILED"
fi

printf 'rate\trun_id\tpostprocess\tstatus\texit_code\treceived_packets\tarrival_rows\tdelay_rows\trecv_span_ms\tclock_sync_samples\taverage_bandwidth_mbps\n' > "$SESSION_DIR/compact_trace_summary.tsv"
for run_dir in "$SESSION_DIR"/dual_udp_latency_downlink_*; do
  [[ -d "$run_dir" ]] || continue
  run_rate="$(awk -F= '$1=="udp_rate" {print $2; exit}' "$run_dir/metadata.txt" 2>/dev/null || true)"
  run_mode="$(awk -F= '$1=="postprocess" {print $2; exit}' "$run_dir/metadata.txt" 2>/dev/null || true)"
  run_status="$(awk -F= '$1=="status" {value=$2} END {print value}' "$run_dir/metadata.txt" 2>/dev/null || true)"
  run_rc="$(tr -d '[:space:]' < "$run_dir/exit_code.txt" 2>/dev/null || true)"
  received="$(awk -F= '$1=="received_packets" {print $2; exit}' "$run_dir/exit_status.txt" 2>/dev/null || true)"
  arrivals="$(awk -F= '$1=="arrival_rows" {print $2; exit}' "$run_dir/exit_status.txt" 2>/dev/null || true)"
  delays="$(awk -F= '$1=="delay_rows" {print $2; exit}' "$run_dir/exit_status.txt" 2>/dev/null || true)"
  span="$(awk -F= '$1=="recv_span_ms" {print $2; exit}' "$run_dir/exit_status.txt" 2>/dev/null || true)"
  clocks="$(awk -F= '$1=="clock_sync_samples" {print $2; exit}' "$run_dir/exit_status.txt" 2>/dev/null || true)"
  bandwidth="$(awk -F= '$1=="average_bandwidth_mbps" {print $2; exit}' "$run_dir/exit_status.txt" 2>/dev/null || true)"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$run_rate" "$(basename "$run_dir")" "$run_mode" "$run_status" "$run_rc" "$received" "$arrivals" "$delays" "$span" "$clocks" "$bandwidth" >> "$SESSION_DIR/compact_trace_summary.tsv"
done

echo "Custom UDP latency sweep outputs saved to: $SESSION_DIR"
exit "$FAILED"
