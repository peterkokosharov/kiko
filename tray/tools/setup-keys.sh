#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Store kiko keys in GNOME Keyring. Values are read without echo and never leave this process.
# Usage: bash tray/tools/setup-keys.sh
set -euo pipefail

ST="${SECRET_TOOL:-/tmp/opencode/mint-deps/root/usr/bin/secret-tool}"
[ -x "$ST" ] || { echo "secret-tool not found: $ST" >&2; exit 1; }

store() {
  local label="$1" service="$2" key="$3" value
  printf '%s [%s]: ' "$key" "$service" >&2
  IFS= read -r -s value || true
  printf '\n' >&2
  [ -n "$value" ] || { printf ' skipped (empty)\n' >&2; return 0; }
  printf '%s' "$value" | "$ST" store --label="$label" service "$service" key "$key"
  printf ' stored in GNOME Keyring\n' >&2
}

echo "Fresh keys only. Old values pasted in chat must be revoked first." >&2
echo >&2
store 'kiko master'      kiko       master
store 'GITHUB_TOKEN'     github     GITHUB_TOKEN
store 'GEMINI_API_KEY'   gemini     GEMINI_API_KEY
store 'NVIDIA_API_KEY'   nvidia     NVIDIA_API_KEY
store 'GROQ_API_KEY'     groq       GROQ_API_KEY
store 'OPENROUTER_API_KEY' openrouter OPENROUTER_API_KEY
store 'MISTRAL_API_KEY'  mistral    MISTRAL_API_KEY

echo >&2
echo "Verify without printing: $ST lookup service kiko key master >/dev/null && echo ok" >&2
