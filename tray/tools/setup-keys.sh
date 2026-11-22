#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Store provider keys in GNOME Keyring. Values are read without echo and never written to disk.
# Layout used by run-kiko.sh: label /peterk/<KEY>, service /peterk, key <KEY>, username peterk.
# Usage: bash tray/tools/setup-keys.sh
set -euo pipefail

ST="${SECRET_TOOL:-secret-tool}"
if ! command -v "$ST" >/dev/null 2>&1 && [ -x /tmp/opencode/mint-deps/root/usr/bin/secret-tool ]; then
  ST=/tmp/opencode/mint-deps/root/usr/bin/secret-tool   # local dev fallback on this machine
fi
KEY_USER="${KIKO_KEY_USER:-$(id -un)}"

store() {
  local key="$1" value
  printf '%s: ' "$key" >&2
  IFS= read -r -s value || true
  printf '\n' >&2
  [ -n "$value" ] || { printf ' skipped (empty)\n' >&2; return 0; }
  printf '%s' "$value" | "$ST" store --label="/peterk/$key" service /peterk key "$key" username "$KEY_USER"
  printf ' stored in GNOME Keyring\n' >&2
}

echo "Keys are read with echo off. Get fresh ones first; never paste a key into chat or git." >&2
echo >&2
store GEMINI_API_KEY
store NVIDIA_API_KEY
store OPENROUTER_API_KEY
store MISTRAL_API_KEY
store GROQ_API_KEY
store GITHUB_TOKEN

echo >&2
echo "Verify without printing a value:" >&2
echo "  $ST lookup service /peterk key NVIDIA_API_KEY username $KEY_USER >/dev/null && echo ok" >&2
