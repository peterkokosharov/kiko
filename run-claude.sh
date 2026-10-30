#!/bin/bash
# run-claude.sh - Launch Claude Code with required environment
# Usage: chmod +x run-claude.sh && ./run-claude.sh
# Or: exec ~/kiko_lite/run-claude.sh
# Or add alias: alias claude='~/kiko_lite/run-claude.sh'

# LiteLLM / Anthropic proxy configuration
export LITELLM_DROP_PARAMS="True"
export ANTHROPIC_BASE_URL="http://127.0.0.1:4000"
# ANTHROPIC_AUTH_TOKEN is required by Claude Code but not used by LiteLLM proxy
# LiteLLM reads API keys from ~/.config/litellm/env or config.yaml
export ANTHROPIC_AUTH_TOKEN="sk-dummy-key"  # placeholder, not used by LiteLLM

# Model routing - set directly here (no bashrc dependency)
export ANTHROPIC_MODEL="nvidia/nemotron-ultra"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="nvidia/nemotron-super"
export ANTHROPIC_DEFAULT_OPUS_MODEL="gemini/gemini-flash-lite"
export ANTHROPIC_DEFAULT_SONNET_MODEL="nvidia/nemotron-ultra"
export ANTHROPIC_DEFAULT_FABLE_MODEL="gemini/gemini-flash"

# Protective flags for Claude Code
export CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1
export DISABLE_PROMPT_CACHING=1

# Add local kikolib to PYTHONPATH
export PYTHONPATH="/home/petark/kiko_lite/kikolib${PYTHONPATH:+:$PYTHONPATH}"

# Launch Claude
exec claude "$@"