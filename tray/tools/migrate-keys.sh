#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Migrate kiko API keys from old secret-tool format to new kiko-identity format.
# Old: service /peterk key NAME username USER
# New: kiko-identity NAME
# Usage: bash tray/tools/migrate-keys.sh

set -euo pipefail

ST="${SECRET_TOOL:-secret-tool}"
if ! command -v "$ST" >/dev/null 2>&1 && [ -x /tmp/opencode/mint-deps/root/usr/bin/secret-tool ]; then
  ST=/tmp/opencode/mint-deps/root/usr/bin/secret-tool
fi
KEY_USER="${KIKO_KEY_USER:-$(id -un)}"

KEYS=(
    GEMINI_API_KEY
    NVIDIA_API_KEY
    OPENROUTER_API_KEY
    MISTRAL_API_KEY
    GROQ_API_KEY
    GITHUB_TOKEN
    MASTER_KEY
)

echo "Migrating kiko API keys to kiko-identity format..."
echo "User: $KEY_USER"
echo

migrated=0
for key in "${KEYS[@]}"; do
    # Try to read from old format
    value=$("$ST" lookup service /peterk key "$key" username "$KEY_USER" 2>/dev/null || true)
    if [ -n "$value" ]; then
        # Store in new format
        printf '%s' "$value" | "$ST" store --label="kiko $key" kiko-identity "$key"
        echo "  migrated: $key"
        migrated=$((migrated + 1))
    else
        # Check if already in new format
        if "$ST" lookup kiko-identity "$key" >/dev/null 2>&1; then
            echo "  already new: $key"
        else
            echo "  not found: $key"
        fi
    fi
done

echo
echo "Done. $migrated key(s) migrated."
echo
echo "Verify with: kiko doctor"