# kiko and CLI tools

kiko is a local gateway on `127.0.0.1:4000`. CLI tools talk to it instead of to a vendor API.
Provider keys stay in GNOME Keyring (`service=/peterk`, `key=<KEY>`, `username=<login>`); the
gateway's management token stays in `~/.kiko/api-token` (0600). No key value belongs in a shell
script, a config file, or this document.

The gateway runs in `--no-auth` home mode on loopback. Clients still need a token because their
SDKs require one, but it is a placeholder (`dummy` / `sk-dummy-key`) that kiko ignores.

## Endpoints used by clients

| Endpoint | Shape | Used by |
|---|---|---|
| `POST /v1/messages` | Anthropic Messages | Claude Code |
| `POST /v1/chat/completions` | OpenAI chat | aider, rex, OpenCode |
| `POST /v1/responses` | OpenAI Responses | Codex CLI |
| `POST /v1beta/models/{model}:generateContent` | Google Gemini | Gemini CLI |
| `POST /v1beta/models/{model}:streamGenerateContent` | Google Gemini (SSE) | Gemini CLI |
| `GET /v1beta/models` | `models[].name` | Gemini CLI |
| `GET /v1/models` | `data[].id` | OpenAI clients, aider |
| `GET /model/info` | `data[].model_name` | goose LiteLLM provider |
| `GET /health` | `{"status":"ok"}` | launcher preflight |
| `GET /ui` | HTML status page | browser |

**Use kiko database aliases, not upstream or LiteLLM names.** For example `nemotron-ultra`,
`llama-nemotron`, `gemini-lite`, `muse-glimmer`. An upstream id such as
`nvidia/nemotron-ultra` resolves to a 404. The verified catalog is in [`MODELS.md`](MODELS.md).

Check the gateway first:

```bash
curl -fsS http://127.0.0.1:4000/health
systemctl --user status kiko-proxy --no-pager
```

The gateway is a systemd **user** service (`~/.config/systemd/user/kiko-proxy.service`, enabled
in `default.target.wants`). The GTK3 tray is an XDG autostart entry
(`~/.config/autostart/kiko-tray.desktop`). Both come back after login.

## Claude Code

Launcher: `claude-in-kiko` (and `claude-in-kiko.sh`) in `~/.local/bin`, pointing at
`~/ai_runtime/run-claude-kiko.sh`. It sets `ANTHROPIC_BASE_URL=http://127.0.0.1:4000`,
`ANTHROPIC_AUTH_TOKEN=sk-dummy-key` and the alias defaults:

| Env var | kiko alias |
|---|---|
| `ANTHROPIC_MODEL` | `nemotron-ultra` |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `gemini-lite` |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `nemotron-ultra` |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `llama-nemotron` |
| `ANTHROPIC_DEFAULT_FABLE_MODEL` | `muse-glimmer` |

`CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1` stays set: Claude Code does not know our
aliases, so it must not guess a compaction boundary.

```bash
claude-in-kiko
claude-in-kiko -p 'Reply with exactly: OK'
claude-in-kiko --resume <session-id>
```

Plain `claude` goes to Anthropic directly, not through kiko.

## aider

Launcher: `aider-in-kiko` (and `aider-in-kiko.sh`) in `~/.local/bin`. It sets
`OPENAI_API_BASE=http://127.0.0.1:4000/v1`, `OPENAI_API_KEY=dummy`, main model
`openai/nemotron-ultra`, weak model `openai/gemini-lite`, and disables model warnings for
unknown aliases.

```bash
cd /path/to/project
aider-in-kiko
aider-in-kiko --model openai/gemini-lite
AIDER_MODEL=openai/gemini-lite aider-in-kiko --yes --no-auto-commits -m "..."
```

Run it from a project directory. `--no-auto-commits` is useful for a first test.

## goose

Use the built-in LiteLLM provider. Its metadata discovery calls `GET /model/info`, which kiko
serves from the same alias list as `/v1/models`.

```bash
goose configure
# Provider: LiteLLM
# LITELLM_API_KEY: dummy
# LITELLM_HOST:  http://127.0.0.1:4000
# Advanced settings: No
# Model: nemotron-ultra
```

If discovery ever fails, goose caches the failure for 60 seconds; retry after that. Do not put a
real key into `LITELLM_API_KEY` — kiko ignores it.

## rex

`rex` uses the same OpenAI-compatible route with `RUNTIME_LITELLM__BASE_URL=http://127.0.0.1:4000`
and a dummy key. Default model is `nemotron-ultra`. The launchers in `~/.local/bin` are:

| Command | Purpose |
|---|---|
| `rex` | interactive CLI |
| `rex-local` | CLI without permission prompts, local demo only |
| `rex-acp` | ACP adapter for IntelliJ (`~/.jetbrains/acp.json`) |

Tools run inside the optional `runtime/guard:latest` sandbox via `sg docker`; use relative paths
such as `src/Main.java` (absolute paths are rejected by the guard). Docker Guard is a tool
sandbox, not the product base.

## OpenCode

OpenCode is not switched over by default. A `kiko` provider is added in
`~/.config/opencode/opencode.json` as an OpenAI-compatible provider with
`baseURL: http://127.0.0.1:4000/v1`; the root `model` is untouched.

```bash
opencode run --model kiko/gemini-lite 'Reply with exactly: OK'
# or inside the TUI: /models -> kiko/nemotron-ultra
```

Verified with `kiko/gemini-lite`: the run printed `OK`.

Launcher: `opencode-in-kiko` (and `opencode-in-kiko.sh`) in `~/.local/bin`. It checks `/health`, then
passes `--model kiko/nemotron-ultra` (override with `OPENCODE_MODEL`) to the TUI or to `run`/`mini`.

```bash
opencode-in-kiko
opencode-in-kiko run 'Reply with exactly: OK'
OPENCODE_MODEL=kiko/gemini-lite opencode-in-kiko run 'Reply with exactly: OK'
```

If the TUI does not pick up the model flag in a future release, select it once with
`/models` → `kiko/nemotron-ultra`; the session keeps the choice.

## Codex CLI

Installed in `~/tools/codex-cli` (`codex-cli 0.171.0`) via npm with a local prefix; `~/tools/codex`
and `~/.local/bin/codex` point at its binary. Plain `codex` keeps its own OpenAI provider.

A Codex profile lives in `~/.codex/kiko.config.toml`: provider `kiko`, `wire_api = "responses"`,
`base_url = http://127.0.0.1:4000/v1`, `env_key = CODEX_KIKO_API_KEY` (the value is a placeholder
set by the launcher, never a real key). Launcher `codex-in-kiko` (and `codex-in-kiko.sh`) in
`~/.local/bin` checks `/health`, sets the dummy key and runs `codex -p kiko`.

```bash
codex-in-kiko exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
  'Reply with exactly: OK'
codex-in-kiko                         # interactive TUI
CODEX_KIKO_MODEL=gemini-lite codex-in-kiko exec '...'
```

Default model is `nemotron-ultra`; override with `CODEX_KIKO_MODEL`. Codex prints a harmless
"Model metadata for ... not found" warning for our aliases and falls back to default metadata.
Verified with `gemini-lite`: `provider: kiko`, output `OK`.

## Gemini CLI

kiko exposes the Google Gemini wire (`gemini_front.py`): `contents`, `systemInstruction`,
`functionDeclarations` and `generationConfig` are converted to the same Anthropic pipeline; the answer is
converted back to `candidates[].content.parts`, `finishReason` and `usageMetadata`. `topK`, `thinkingConfig`
and other Gemini-only knobs are dropped, not forwarded. Both `v1beta` and `v1alpha` paths are served.

Launcher: `gemini-in-kiko` (and `gemini-in-kiko.sh`) in `~/.local/bin`. It checks `/health`, sets
`GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:4000`, sets `GEMINI_API_KEY=dummy`, unsets `GOOGLE_API_KEY`
(so the CLI cannot prompt or pull the real key from keyring), and runs `gemini -m gemini-lite`.

```bash
gemini-in-kiko -p 'Reply with exactly: OK'   # one-shot; add --skip-trust for a new temp folder
gemini-in-kiko                               # interactive TUI
GEMINI_KIKO_MODEL=llama-nemotron gemini-in-kiko
```

**Do not use `/model` inside the TUI to switch.** The picker lists native Google models such as
`gemini-3.5-flash-lite`; kiko does not have them in its DB and answers 404. The model is fixed at launch
with `-m` or `GEMINI_KIKO_MODEL`. Restart the CLI session after changing it. Verified with
`gemini-lite`: one-shot `-p` printed `OK`.

Plain `gemini` still goes directly to Google with the real key from GNOME Keyring. The `~/.local/bin/gemini`
wrapper reads that key only when neither `GEMINI_API_KEY` nor `GOOGLE_API_KEY` is set.

## rex and Layka in a terminal

Both work from IntelliJ's terminal (and any shell); `~/.local/bin` is in PATH from `~/.profile` / `~/.bashrc`.

| Command | What it is |
|---|---|
| `rex`, `rex-local`, `rex-acp` | launchers in `~/.local/bin`; `REX_ROOT` points at the stable source in `~/tools/rex` (v0.2.1) |
| `layka` | symlink in `~/.local/bin` to `~/Documents/Default Project/project-layka/build/layka` |

The old `REX_ROOT=/tmp/opencode/rex-native-port` broke after `/tmp` was cleaned (`No module named runtime`);
it now points at `~/tools/rex`. rex uses kiko at `http://127.0.0.1:4000` with a dummy key, model
`nemotron-ultra`; tools run in `runtime/guard:latest` via `sg docker`. Verified in `~/archive`: `rex`
answered `OK` and created a file via the fileops tool. `rex-local`/`--local-tools` is demo only.
Layka is a Turbo Vision TUI, so it needs a real terminal (it was started successfully in a terminal).

## Still on their own models

- **Gemini CLI** — plain `gemini` uses its own key and native models; use `gemini-in-kiko` for kiko.
- **Codex CLI** — plain `codex` uses its own OpenAI models; use `codex-in-kiko` for kiko.
- **OpenCode** — keeps its configured default model; `kiko/...` is available when selected.

## kiko itself

```bash
kiko doctor      # key NAMES only, never values
kiko models      # refresh the verified catalog, keeps the DB
kiko restart     # restart the user service
```

`kiko install` needs no sudo; sudo is only for `dpkg -i` of the packages.
