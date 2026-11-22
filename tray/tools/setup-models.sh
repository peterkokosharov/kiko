#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Add the verified free-tier model catalog to a kiko database. Keys are never read or copied:
# only their NAMES go into the DB; values stay in each machine's OS keyring.
# Usage: bash tray/tools/setup-models.sh [--db PATH]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GW="$ROOT/gateway"
DB="${HOME}/.kiko/kiko.db"
if [ "${1:-}" = "--db" ]; then DB="${2:?path after --db}"; fi

kiko() { PYTHONPATH="$GW" python3 -m kiko_lite "$@" --db "$DB" 2>&1 | grep -v 'no price known' || true; }
add()  { kiko add-model "$1" --provider "$2" --api-key-env "$3" --class "$4" "${@:5}"; }

# OpenRouter rows need a custom provider with a fixed base (only OpenAI honours a row's api_base).
kiko add-provider openrouter --api-base https://openrouter.ai/api/v1 --api-key-env OPENROUTER_API_KEY

# OPUS — manager / evaluator
add nemotron-ultra   nvidia/nvidia/nemotron-3-ultra-550b-a55b  NVIDIA_API_KEY   OPUS
add gpt-astra        openrouter/openai/gpt-6.2-astra           OPENROUTER_API_KEY OPUS --api-base https://openrouter.ai/api/v1
# SONNET — main implementation work
add nemotron-super   nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b NVIDIA_API_KEY SONNET
add llama-nemotron   nvidia/nvidia/nemotron-3.5-lightning-30b-a3b NVIDIA_API_KEY SONNET --thinking off
add nvidia-large     nvidia/mistralai/mistral-large-4-1t-a52b   NVIDIA_API_KEY   SONNET
# HAIKU — bulk simple work (fastest first)
add gemini-lite      gemini/gemini-flash-lite-latest           GEMINI_API_KEY   HAIKU
add gemini-35-lite   gemini/gemini-3.5-flash-lite             GEMINI_API_KEY   HAIKU
add ministral-3b     mistral/ministral-3b-latest              MISTRAL_API_KEY  HAIKU
# FABLE — external auditor
add muse-glimmer     nvidia/meta/muse-glimmer-30b             NVIDIA_API_KEY   FABLE
# CODER / EYES / TUTOR / ROUTER
add codestral        mistral/codestral-latest                 MISTRAL_API_KEY  CODER
add nemotron-vl      nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b NVIDIA_API_KEY EYES --thinking off
add gemini-tutor     gemini/gemini-3.5-flash-lite             GEMINI_API_KEY   TUTOR
add router-mini      mistral/ministral-3b-latest              MISTRAL_API_KEY  ROUTER

# Known not to answer today on the free tier: keep them visible but off.
for m in starcoder2 gemma-3-12b gemma-4-31b minimax-flash gemini-flash gemini-pro-preview mistral-small; do
  kiko disable-model "$m"
done
kiko list-models
