#!/usr/bin/env bash
set -euo pipefail

# Install the bundled Douyin internal-test APK on an already connected device.
# Usage:
#   ./install_douyin_test_app.sh [APK_PATH]
#   ADB_DEVICE=<serial> ./install_douyin_test_app.sh

fail() {
  echo "[install] error: $*" >&2
  exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v adb >/dev/null 2>&1 || fail "adb not found. Install adb and connect the phone first."

if [[ $# -gt 1 ]]; then
  fail "usage: $0 [APK_PATH]"
fi

if [[ $# -eq 1 ]]; then
  APK_PATH="$1"
else
  shopt -s nullglob
  APK_CANDIDATES=("$SCRIPT_DIR"/bin/app_local_test*.apk)
  shopt -u nullglob

  if [[ ${#APK_CANDIDATES[@]} -eq 0 ]]; then
    fail "no app_local_test APK found under $SCRIPT_DIR/bin"
  fi
  if [[ ${#APK_CANDIDATES[@]} -gt 1 ]]; then
    fail "multiple app_local_test APKs found; pass the APK path explicitly"
  fi
  APK_PATH="${APK_CANDIDATES[0]}"
fi

[[ -f "$APK_PATH" ]] || fail "APK not found: $APK_PATH"

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
echo "[install] APK: $APK_PATH"
echo "[install] installing..."

adb -s "$DEVICE" install -r "$APK_PATH"

echo "[install] done"
