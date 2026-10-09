#!/usr/bin/env bash
set -euo pipefail

LOCAL_IPERF="${1:-}"
REMOTE_IPERF="${2:-/data/local/tmp/iperf3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${BASE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LOG_ROOT="$BASE_DIR/logs"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IPERF_BIN_DIR="${IPERF_BIN_DIR:-$REPO_ROOT/runtime/android/bin}"
fail() { echo "ERROR: $*" >&2; exit 1; }
[[ -z "$LOCAL_IPERF" || -f "$LOCAL_IPERF" ]] || fail "local iperf3 binary not found: $LOCAL_IPERF"
command -v adb >/dev/null 2>&1 || fail "adb not found"
DEVICES=()
while IFS= read -r device; do
  [[ -n "$device" ]] && DEVICES+=("$device")
done < <(adb devices | awk '$2 == "device" {print $1}')
[[ ${#DEVICES[@]} -eq 2 ]] || fail "expected exactly two ready adb devices, found ${#DEVICES[@]}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"; RUN_DIR="$LOG_ROOT/iperf_install_two_phones_$TS"; mkdir -p "$RUN_DIR"
adb devices -l > "$RUN_DIR/adb_devices.txt" 2>&1 || true
getprop() { adb -s "$1" shell "getprop $2" 2>/dev/null | tr -d '\r'; }
{
  echo "local_iperf=${LOCAL_IPERF:-auto_by_device_abi}"; echo "iperf_bin_dir=$IPERF_BIN_DIR"; echo "remote_iperf=$REMOTE_IPERF"
  for device in "${DEVICES[@]}"; do
    manufacturer="$(getprop "$device" ro.product.manufacturer)"; model="$(getprop "$device" ro.product.model)"
    abi="$(getprop "$device" ro.product.cpu.abi)"; abilist="$(getprop "$device" ro.product.cpu.abilist)"
    printf 'device=%s manufacturer=%s model=%s abi=%s abilist=%s state=device\n' "$device" "$manufacturer" "$model" "$abi" "$abilist"
    printf 'serial=%s\nmanufacturer=%s\nmodel=%s\nabi=%s\nabilist=%s\n' "$device" "$manufacturer" "$model" "$abi" "$abilist" > "$RUN_DIR/phone_${device//[:\/]/_}.txt"
    echo "[device] serial=$device manufacturer=${manufacturer:-unknown} model=${model:-unknown} abi=${abi:-unknown}"
  done
} | tee "$RUN_DIR/device_mapping.txt"
for device in "${DEVICES[@]}"; do
  abilist="$(getprop "$device" ro.product.cpu.abilist)"
  DEVICE_IPERF="$LOCAL_IPERF"
  if [[ -z "$DEVICE_IPERF" ]]; then
    case "$abilist" in
      *arm64-v8a*) DEVICE_IPERF="$IPERF_BIN_DIR/iperf3.21-arm64-v8a" ;;
      *armeabi-v7a*) DEVICE_IPERF="$IPERF_BIN_DIR/iperf3.21-armeabi-v7a" ;;
      *) fail "no bundled iperf3 matches $device ABI list: $abilist" ;;
    esac
  fi
  [[ -f "$DEVICE_IPERF" ]] || fail "iperf3 binary not found for $device: $DEVICE_IPERF"
  case "$DEVICE_IPERF:$abilist" in *arm64-v8a*:*arm64-v8a*) ;; *arm64-v8a*:*) fail "$device does not support arm64-v8a: $abilist" ;; esac
  echo "[install] $device: pushing $DEVICE_IPERF -> $REMOTE_IPERF"
  adb -s "$device" push "$DEVICE_IPERF" "$REMOTE_IPERF" > "$RUN_DIR/push_${device//[:\/]/_}.txt"
  adb -s "$device" shell "chmod 755 '$REMOTE_IPERF'"
  adb -s "$device" shell "'$REMOTE_IPERF' -v" > "$RUN_DIR/verify_${device//[:\/]/_}.txt" 2>&1
  grep -qi 'iperf' "$RUN_DIR/verify_${device//[:\/]/_}.txt" || fail "iperf3 verification failed on $device"
done
printf 'type=two_phone_iperf_install\nlocal_iperf=%s\niperf_bin_dir=%s\nremote_iperf=%s\n' "${LOCAL_IPERF:-auto_by_device_abi}" "$IPERF_BIN_DIR" "$REMOTE_IPERF" > "$RUN_DIR/metadata.txt"
echo "[install] done; mapping saved to $RUN_DIR/device_mapping.txt"
echo "[install] run with: $SCRIPT_DIR/run_douyin_capacity.sh ${DEVICES[0]} <server-ip>"
