#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Start kiko-lite with provider keys read straight from GNOME Keyring (never written to disk).
# Usage: bash tray/tools/run-kiko.sh [extra kiko serve args]
set -euo pipefail

ST="${SECRET_TOOL:-secret-tool}"
if ! command -v "$ST" >/dev/null 2>&1 && [ -x /tmp/opencode/mint-deps/root/usr/bin/secret-tool ]; then
  ST=/tmp/opencode/mint-deps/root/usr/bin/secret-tool   # local dev fallback on this machine
fi
KEY_USER="${KIKO_KEY_USER:-$(id -un)}"
get() { "$ST" lookup service /peterk key "$1" username "$KEY_USER" 2>/dev/null || true; }

KIKO_DIR="${KIKO_DIR:-$(cd "$(dirname "$0")/../../gateway" && pwd)}"
export GEMINI_API_KEY="$(get GEMINI_API_KEY)"
export NVIDIA_API_KEY="$(get NVIDIA_API_KEY)"
export GROQ_API_KEY="$(get GROQ_API_KEY)"
export OPENROUTER_API_KEY="$(get OPENROUTER_API_KEY)"
export MISTRAL_API_KEY="$(get MISTRAL_API_KEY)"

cd "$KIKO_DIR"
export PYTHONPATH=.
exec python3 -m kiko_lite serve --no-auth "$@"
