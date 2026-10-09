#!/usr/bin/env bash
set -euo pipefail

# Push one complex URL to an Android text file and try to open it.
# Usage:
#   ./send_url_to_phone.sh 'https://example.com/path?a=1&b=2'
#   printf '%s' "$URL" | ./send_url_to_phone.sh
#   ADB_DEVICE=<serial> ./send_url_to_phone.sh "$URL"

fail() {
  echo "[send-url] error: $*" >&2
  exit 1
}

usage() {
  echo "usage: $0 [URL]" >&2
  echo "       printf '%s' \"\$URL\" | $0" >&2
}

if [[ $# -gt 1 ]]; then
  usage
  fail "expected zero or one argument"
fi

REMOTE_PATH="${REMOTE_PATH:-/sdcard/Download/url_from_mac.txt}"
[[ "$REMOTE_PATH" == /* ]] || fail "REMOTE_PATH must be an absolute Android path: $REMOTE_PATH"
[[ "$REMOTE_PATH" =~ ^[A-Za-z0-9_./-]+$ ]] || fail "REMOTE_PATH contains unsupported characters: $REMOTE_PATH"

LOCAL_FILE="$(mktemp "${TMPDIR:-/tmp}/send_url_to_phone.XXXXXX")"
cleanup() {
  rm -f "$LOCAL_FILE"
}
trap cleanup EXIT INT TERM

if [[ $# -eq 1 ]]; then
  printf '%s' "$1" > "$LOCAL_FILE"
elif [[ ! -t 0 ]]; then
  dd of="$LOCAL_FILE" bs=64k 2>/dev/null
else
  usage
  fail "URL argument or stdin input is required"
fi

[[ -s "$LOCAL_FILE" ]] || fail "URL must not be empty"
LOCAL_BYTES="$(wc -c < "$LOCAL_FILE" | tr -d ' ')"
NON_NEWLINE_BYTES="$(tr -d '\n' < "$LOCAL_FILE" | wc -c | tr -d ' ')"
[[ "$LOCAL_BYTES" == "$NON_NEWLINE_BYTES" ]] || fail "URL must be a single line without newline characters"

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
echo "[send-url] device: $DEVICE${MODEL:+ ($MODEL)}"
echo "[send-url] bytes: $LOCAL_BYTES"
echo "[send-url] destination: $REMOTE_PATH"

adb -s "$DEVICE" push "$LOCAL_FILE" "$REMOTE_PATH" >/dev/null
adb -s "$DEVICE" shell "test -f '$REMOTE_PATH'" >/dev/null 2>&1 || fail "remote file was not created: $REMOTE_PATH"
REMOTE_BYTES="$(adb -s "$DEVICE" shell "wc -c < '$REMOTE_PATH'" 2>/dev/null | tr -d '[:space:]')"
[[ "$REMOTE_BYTES" == "$LOCAL_BYTES" ]] || fail "remote size mismatch: local=$LOCAL_BYTES remote=${REMOTE_BYTES:-unknown}"

if [[ "$REMOTE_PATH" == /sdcard/* ]]; then
  DOCUMENT_PATH="${REMOTE_PATH#/sdcard/}"
  DOCUMENT_PATH="${DOCUMENT_PATH//\//%2F}"
  VIEW_URI="content://com.android.externalstorage.documents/document/primary%3A$DOCUMENT_PATH"
else
  VIEW_URI="file://$REMOTE_PATH"
fi

set +e
OPEN_OUTPUT="$(adb -s "$DEVICE" shell am start -a android.intent.action.VIEW -d "$VIEW_URI" -t text/plain -f 0x1 2>&1)"
OPEN_RC=$?
set -e

if [[ "$OPEN_RC" -ne 0 ]] || printf '%s' "$OPEN_OUTPUT" | grep -Eqi 'error:|unable to resolve|no activity found'; then
  echo "[send-url] warning: could not open a text viewer automatically" >&2
  echo "[send-url] open the file from Files/Downloads: $REMOTE_PATH" >&2
else
  echo "[send-url] opened the text file; long-press the URL to copy it"
fi

echo "[send-url] done"
