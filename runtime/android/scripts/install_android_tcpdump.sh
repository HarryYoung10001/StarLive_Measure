#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./install_android_tcpdump.sh /path/to/android/tcpdump [REMOTE_PATH]
#
# Purpose:
#   Push an Android-compatible tcpdump binary to the connected Reemo/Android
#   cloud phone and make it executable.

LOCAL_TCPDUMP="${1:-}"
REMOTE_TCPDUMP="${2:-/data/local/tmp/tcpdump}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

if [ -z "$LOCAL_TCPDUMP" ]; then
  fail "missing local Android tcpdump binary. Usage: $0 /path/to/android/tcpdump [REMOTE_PATH]"
fi

if [ ! -f "$LOCAL_TCPDUMP" ]; then
  fail "local tcpdump binary not found: $LOCAL_TCPDUMP"
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

case "$LOCAL_TCPDUMP:$DEVICE_ABILIST" in
  *arm64-v8a*:*arm64-v8a*) ;;
  *arm64-v8a*:*) fail "binary is arm64-v8a, but device supports: $DEVICE_ABILIST" ;;
  *armeabi-v7a*:*) ;;
esac

echo "[install] pushing $LOCAL_TCPDUMP -> $REMOTE_TCPDUMP"
adb -s "$DEVICE" push "$LOCAL_TCPDUMP" "$REMOTE_TCPDUMP"

echo "[install] chmod +x $REMOTE_TCPDUMP"
adb -s "$DEVICE" shell "chmod 755 '$REMOTE_TCPDUMP'"

echo "[install] verifying binary"
VERIFY_OUTPUT="$(adb -s "$DEVICE" shell "'$REMOTE_TCPDUMP' --version" 2>&1 | tr -d '\r' || true)"
if ! printf '%s\n' "$VERIFY_OUTPUT" | grep -qi 'tcpdump'; then
  VERIFY_OUTPUT="$(adb -s "$DEVICE" shell "'$REMOTE_TCPDUMP' -h" 2>&1 | tr -d '\r' || true)"
fi
echo "$VERIFY_OUTPUT"

case "$VERIFY_OUTPUT" in
  *"not executable"*|*"Permission denied"*|*"No such file"*|*"inaccessible"*|*"64-bit ELF file"*)
    fail "verification failed; check that the binary ABI matches the device ABI"
    ;;
esac

if ! printf '%s\n' "$VERIFY_OUTPUT" | grep -qi 'tcpdump'; then
  fail "verification did not print tcpdump help/version output"
fi

echo "[install] checking capture interfaces"
adb -s "$DEVICE" shell "'$REMOTE_TCPDUMP' -D" 2>&1 | tr -d '\r' || true

echo "[install] done"
echo "[install] note: packet capture may still require root. If capture fails, run:"
echo "  TCPDUMP_PREFIX='su -c' ./runtime/android/scripts/run_downlink_tcpdump_loss_phone.sh ..."
