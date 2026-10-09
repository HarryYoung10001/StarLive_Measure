#!/usr/bin/env bash
set -euo pipefail

# Install the bundled push/pull streaming demo APKs on one ADB device.
# Usage:
#   ./install_stream_demo_apps.sh [push|pull]
#   ADB_DEVICE=<serial> ./install_stream_demo_apps.sh [push|pull]

fail() {
  echo "[install] error: $*" >&2
  exit 1
}

usage() {
  echo "usage: $0 [push|pull]" >&2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUSH_APK="$SCRIPT_DIR/bin/PushDemo/app-debug.apk"
PULL_APK="$SCRIPT_DIR/bin/PullDemo/app-aweme-debug.apk"
MODE="${1:-all}"

if [[ $# -gt 1 ]]; then
  usage
  fail "too many arguments"
fi

case "$MODE" in
  all)
    APP_NAMES=(push pull)
    APK_PATHS=("$PUSH_APK" "$PULL_APK")
    ;;
  push)
    APP_NAMES=(push)
    APK_PATHS=("$PUSH_APK")
    ;;
  pull)
    APP_NAMES=(pull)
    APK_PATHS=("$PULL_APK")
    ;;
  *)
    usage
    fail "invalid mode: $MODE"
    ;;
esac

for apk_path in "${APK_PATHS[@]}"; do
  [[ -f "$apk_path" ]] || fail "APK not found: $apk_path"
done

command -v adb >/dev/null 2>&1 || fail "adb not found. Install adb and connect the phone first."

if [[ -n "${ADB_DEVICE:-}" ]]; then
  DEVICE="$ADB_DEVICE"
  DEVICE_STATE="$(adb -s "$DEVICE" get-state 2>/dev/null || true)"
  [[ "$DEVICE_STATE" == "device" ]] || fail "adb device is not ready: $DEVICE"
else
  DEVICE_LIST="$(adb devices | awk '$2 == "device" {print $1}')"
  DEVICE_COUNT="$(printf '%s\n' "$DEVICE_LIST" | awk 'NF {count++} END {print count + 0}')"
  if [[ "$DEVICE_COUNT" -eq 0 ]]; then
    fail "no adb device found. Connect the phone first and run: adb devices"
  fi
  if [[ "$DEVICE_COUNT" -gt 1 ]]; then
    fail "multiple adb devices found; select one with ADB_DEVICE=<serial>"
  fi
  DEVICE="$DEVICE_LIST"
fi

MODEL="$(adb -s "$DEVICE" shell getprop ro.product.model 2>/dev/null | tr -d '\r')"
echo "[install] device: $DEVICE${MODEL:+ ($MODEL)}"
echo "[install] mode: $MODE"

for index in "${!APK_PATHS[@]}"; do
  app_name="${APP_NAMES[$index]}"
  apk_path="${APK_PATHS[$index]}"
  echo "[install] $app_name APK: $apk_path"
  echo "[install] installing $app_name..."
  adb -s "$DEVICE" install -r "$apk_path"
done

echo "[install] done"
