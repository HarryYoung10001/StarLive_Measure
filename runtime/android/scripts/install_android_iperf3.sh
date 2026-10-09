#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./install_android_iperf3.sh /path/to/android/iperf3 [REMOTE_PATH]
#
# Purpose:
#   Push an Android-compatible iperf3 binary to the connected Reemo/Android
#   cloud phone and make it executable.

LOCAL_IPERF="${1:-}"
REMOTE_IPERF="${2:-/data/local/tmp/iperf3}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

if [ -z "$LOCAL_IPERF" ]; then
  fail "missing local Android iperf3 binary. Usage: $0 /path/to/android/iperf3 [REMOTE_PATH]"
fi

if [ ! -f "$LOCAL_IPERF" ]; then
  fail "local iperf3 binary not found: $LOCAL_IPERF"
fi

command -v adb >/dev/null 2>&1 || fail "adb not found on Mac. Connect Reemo first, then retry."

DEVICE="${ADB_DEVICE:-$(adb devices | awk '$2=="device"{print $1; exit}')}"

if [ -z "$DEVICE" ]; then
  fail "no adb device found. Run: hb adb connect -d <DEVICE_ID>"
fi

echo "[install] device: $DEVICE"
DEVICE_ABI="$(adb -s "$DEVICE" shell "getprop ro.product.cpu.abi" | tr -d '\r')"
DEVICE_ABILIST="$(adb -s "$DEVICE" shell "getprop ro.product.cpu.abilist" | tr -d '\r')"
echo "[install] Android ABI: $DEVICE_ABI"
echo "[install] Android ABI list: $DEVICE_ABILIST"

case "$LOCAL_IPERF:$DEVICE_ABILIST" in
  *arm64-v8a*:*arm64-v8a*) ;;
  *arm64-v8a*:*) fail "binary is arm64-v8a, but device supports: $DEVICE_ABILIST" ;;
  *armeabi-v7a*:*) ;;
esac

echo "[install] pushing $LOCAL_IPERF -> $REMOTE_IPERF"
adb -s "$DEVICE" push "$LOCAL_IPERF" "$REMOTE_IPERF"

echo "[install] chmod +x $REMOTE_IPERF"
adb -s "$DEVICE" shell "chmod 755 '$REMOTE_IPERF'"

echo "[install] verifying"
VERIFY_OUTPUT="$(adb -s "$DEVICE" shell "'$REMOTE_IPERF' -v" 2>&1 | tr -d '\r' || true)"
echo "$VERIFY_OUTPUT"

case "$VERIFY_OUTPUT" in
  *"not executable"*|*"Permission denied"*|*"No such file"*|*"inaccessible"*)
    fail "verification failed; check that the binary ABI matches the device ABI"
    ;;
esac

if ! printf '%s\n' "$VERIFY_OUTPUT" | grep -qi 'iperf'; then
  fail "verification did not print an iperf version"
fi

echo "[install] done"
