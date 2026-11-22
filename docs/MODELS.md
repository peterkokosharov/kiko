# Verified free-tier model catalog

Measured on 2026-11-22 against `kiko-lite` on Linux Mint 22.3. Every row answered a live
`POST /v1/chat/completions` with `Reply with exactly: OK` through the gateway. Run
`bash tray/tools/setup-models.sh` to install this catalog on another laptop; it writes only
key NAMES to the DB. Provider key VALUES stay in the local OS keyring.

The starting point was [llmhub's models.json](https://github.com/peterkokosharov/llmhub/blob/main/models.json).
NVIDIA rotates model ids, so the ids below are cross-checked against the live `/v1/models` list
of each provider, not copied from that file. Where llmhub's name no longer exists, the current
equivalent is used (e.g. `nemotron-ultra-253b-instruct` → `nemotron-3-ultra-550b-a55b`).

| Alias | Class | Provider / upstream id | Key name | Result |
|---|---|---|---|---|
| `nemotron-ultra` | OPUS | nvidia/nvidia/nemotron-3-ultra-550b-a55b | NVIDIA_API_KEY | OK, 45 tok |
| `gpt-astra` | OPUS | openrouter/openai/gpt-6.2-astra | OPENROUTER_API_KEY | OK, 16 tok |
| `nemotron-super` | SONNET | nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b | NVIDIA_API_KEY | OK, 44 tok |
| `llama-nemotron` | SONNET | nvidia/nvidia/nemotron-3.5-lightning-30b-a3b | NVIDIA_API_KEY | OK with `--thinking off`, 23 tok |
| `nvidia-large` | SONNET | nvidia/mistralai/mistral-large-4-1t-a52b | NVIDIA_API_KEY | OK, 33 tok |
| `gemini-lite` | HAIKU | gemini/gemini-flash-lite-latest | GEMINI_API_KEY | OK, 7 tok — fastest |
| `gemini-35-lite` | HAIKU | gemini/gemini-3.5-flash-lite | GEMINI_API_KEY | OK, 7 tok |
| `ministral-3b` | HAIKU | mistral/ministral-3b-latest | MISTRAL_API_KEY | OK, 10 tok |
| `muse-glimmer` | FABLE | nvidia/meta/muse-glimmer-30b | NVIDIA_API_KEY | OK, 162 tok |
| `codestral` | CODER | mistral/codestral-latest | MISTRAL_API_KEY | OK, 10 tok |
| `nemotron-vl` | EYES | nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b | NVIDIA_API_KEY | OK with `--thinking off`, 23 tok (text checked; image input not checked) |
| `gemini-tutor` | TUTOR | gemini/gemini-3.5-flash-lite | GEMINI_API_KEY | OK, 7 tok |
| `router-mini` | ROUTER | mistral/ministral-3b-latest | MISTRAL_API_KEY | OK, 10 tok |

## Kept but disabled

| Alias | Why |
|---|---|
| `starcoder2` | NVIDIA 404 — completion model, not a chat endpoint |
| `gemma-3-12b` | NVIDIA 404 "function not found" for this account |
| `gemma-4-31b`, `minimax-flash`, `gemini-flash` (3.8) | timed out at 75 s |
| `gemini-pro-preview` | Gemini free quota exhausted (429) |
| `mistral-small` | Mistral rate limit (429) |

Disabled models stay in the database so the tray shows them as off; they are never picked by
a class alias. Class aliases `opus`, `sonnet`, `haiku`, `fable` and `kiko-coder` all answered
through their enabled candidates in the same test run.

## Groq

`GROQ_API_KEY` returned 403 on `/v1/models` on this machine — no Groq rows are configured.
