# kiko on Linux Mint — Complete Install Guide

> **Policy**: Everything is documented. New machine → `kikolib` → `kiko-lite` → `kiko-tray` → done.

---

## Quick Start (TL;DR)

```bash
# 1. Clone
git clone https://github.com/peterkokosharov/kiko.git ~/kiko
cd ~/kiko

# 2. Install gateway (kikolib + kiko-lite) + tray + systemd service
bash tray/tools/kiko install

# 3. Verify
kiko doctor
```

That's it. You now have:
- **Gateway** on `http://127.0.0.1:4000` (Anthropic-compatible API)
- **Tray icon** in your panel (green = running)
- **Systemd user service** (auto-starts on login)
- **13 pre-configured models** across OPUS/SONNET/HAIKU/FABLE/CODER/EYES/TUTOR/ROUTER classes

---

## Prerequisites

```bash
sudo apt update
sudo apt install -y \
  python3.12 python3.12-venv \
  libsecret-tools \
  libgtk-3-dev libxapp-dev \
  cmake build-essential pkg-config \
  libglib2.0-dev libcairo2-dev libpango1.0-dev
```

| Package | Why |
|---------|-----|
| `python3.12-venv` | Required for test suite (venv module) |
| `libsecret-tools` | `secret-tool` CLI for GNOME Keyring (API keys) |
| `libxapp-dev` | `XAppStatusIcon` (native Cinnamon tray, better than GtkStatusIcon) |
| `cmake + build-essential` | Build `kiko-tray` native binary |
| `gtk-3-dev` | GTK3 tray fallback if XApp not available |

---

## Step-by-Step

### 1. Clone the Repo

```bash
git clone https://github.com/peterkokosharov/kiko.git ~/kiko
cd ~/kiko
```

### 2. Run the Installer

```bash
bash tray/tools/kiko install
```

**What this does:**

| Action | Details |
|--------|---------|
| Copies gateway | `~/kiko/gateway/kikolib` + `~/kiko/gateway/kiko_lite` → `~/.local/share/kiko/gateway/` |
| Copies tray tools | `~/kiko/tray/tools/` → `~/.local/share/kiko/tray/tools/` |
| Builds `kiko-tray` | `cmake -B build -S tray && cmake --build build` (GTK3 + XApp) |
| Installs binary | `~/.local/share/kiko/tray/tools/kiko-tray` → `~/.local/bin/kiko-tray` |
| Creates wrapper | `~/.local/bin/kiko` → points to `~/.local/share/kiko/tray/tools/kiko` |
| Prompts for API keys | `GEMINI_API_KEY`, `NVIDIA_API_KEY` (stored in GNOME Keyring) |
| Creates database | `~/.kiko/kiko.db` with 13 verified models |
| Installs systemd service | `~/.config/systemd/user/kiko-lite.service` |
| Starts service | `systemctl --user enable --now kiko-lite` |
| Health check | Waits for `http://127.0.0.1:4000/health` |

**You'll be prompted for keys** (echo off, never written to disk):
```
GEMINI_API_KEY: **************** stored in GNOME Keyring
NVIDIA_API_KEY: **************** stored in GNOME Keyring
```

### 3. Verify

```bash
kiko doctor
```

Expected output:
```
kiko doctor
  python3      : Python 3.12.x
  code root    : /home/you/.local/share/kiko
  database     : /home/you/.kiko/kiko.db
  secret-tool  : /usr/bin/secret-tool
  NVIDIA_API_KEY: present
  GEMINI_API_KEY: present
  service      : active
  health :4000 : ok
```

---

## The Tray Icon

### What You See

```
┌─────────────────────────────────────────┐
│  ● kiko: running (port 4000)            │  ← GREEN = KT_STATE_OK
│  ─────────────────────────────────────  │
│  Start kiko          (disabled)         │
│  Stop kiko                           │
│  Restart kiko                        │
│  ─────────────────────────────────────  │
│  Status                              │
│  Logs                                │
│  Models                              │
│  Model classes                       │
│  Keys                                │
│  Costs                               │
│  Settings                            │
│  About                               │
│  ─────────────────────────────────────  │
│  Quit                                │
└─────────────────────────────────────────┘
```

### Icon States (rendered by `kt_icon.c`)

| State | Color | Hex | When |
|-------|-------|-----|------|
| **OK** | ![#2EA043](https://placehold.co/15x15/2EA043/2EA043.png) Green | `#2EA043` | Gateway healthy (`/health` = 200, status "ok") |
| **DEGRADED** | ![#F08800](https://placehold.co/15x15/F08800/F08800.png) Orange | `#F08800` | Gateway responds but status ≠ "ok" |
| **DOWN** | ![#D62828](https://placehold.co/15x15/D62828/D62828.png) Red | `#D62828` | No answer on port 4000 |
| **STARTING** | ![#2478E6](https://placehold.co/15x15/2478E6/2478E6.png) Blue | `#2478E6` | Within 30s grace period after Start/Restart |

The icon is a **white "K" on a colored circle** — rendered in-memory by `kt_icon_render()` (no image files needed).

### On Linux Mint (Cinnamon)

- Uses **`XAppStatusIcon`** (native panel integration, supports HiDPI)
- Right-click = menu, Left-click = Status dialog
- Tooltip shows current state

### On XFCE / GNOME / Others

- Falls back to **`GtkStatusIcon`** (deprecated but works)
- Same menu, same icons
- Notifications via `notify-send`

---

## Using the Gateway

### With Claude Code

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_AUTH_TOKEN=your-master-key  # or use --no-auth mode
claude
```

### With Any OpenAI-Compatible Client

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer your-master-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"sonnet","messages":[{"role":"user","content":"hi"}]}'
```

### Model Classes (Routing)

| Class | Keyword | Role | Example Models |
|-------|---------|------|----------------|
| **OPUS** | `opus` | Manager/Evaluator | `nemotron-ultra`, `gpt-astra` |
| **SONNET** | `sonnet` | Main Implementation | `nemotron-super`, `llama-nemotron`, `nvidia-large` |
| **HAIKU** | `haiku` | Bulk/Simple/Fast | `gemini-lite`, `gemini-35-lite`, `ministral-3b` |
| **FABLE** | `fable` | External Auditor | `muse-glimmer` |
| **CODER** | `coder` | Code Generation | `codestral` |
| **EYES** | `eyes` | Vision | `nemotron-vl` |
| **TUTOR** | `tutor` | Teaching | `gemini-tutor` |
| **ROUTER** | `router` | Routing | `router-mini` |

Claude Code asks for a **CLASS** (OPUS/SONNET/HAIKU/FABLE), kiko-lite picks an **alive model** of that class.

---

## Managing Keys

Keys live in **GNOME Keyring** (never in config files, never in DB, never in logs).

```bash
# List stored keys
secret-tool search kiko-identity NVIDIA_API_KEY
secret-tool search kiko-identity GEMINI_API_KEY

# Add a key manually
secret-tool store --label="kiko GROQ_API_KEY" kiko-identity GROQ_API_KEY

# Remove a key
secret-tool clear kiko-identity GROQ_API_KEY
```

The tray **Keys** dialog shows only: key name + present/absent (values never fetched).

---

## Common Commands

```bash
kiko doctor          # Full health check
kiko models          # Re-run verified model catalog
kiko start           # Start gateway (systemd)
kiko stop            # Stop gateway
kiko restart         # Restart gateway
kiko status          # systemctl status
kiko uninstall       # Remove service + code (--purge also removes DB)
```

### Gateway CLI (direct)

```bash
# From the gateway source
cd ~/.local/share/kiko/gateway
python3 -m kiko_lite --help

# Useful commands
python3 -m kiko_lite list-models
python3 -m kiko_lite costs
python3 -m kiko_lite add-model my-model --provider openai/gpt-4o --api-key-env OPENAI_API_KEY --class SONNET
python3 -m kiko_lite set-setting monthly_cap_usd 50
```

---

## File Locations

| What | Path |
|------|------|
| Gateway source (copied) | `~/.local/share/kiko/gateway/` |
| Tray tools | `~/.local/share/kiko/tray/tools/` |
| Tray binary | `~/.local/bin/kiko-tray` |
| Wrapper script | `~/.local/bin/kiko` |
| Database | `~/.kiko/kiko.db` (SQLite) |
| Systemd unit | `~/.config/systemd/user/kiko-lite.service` |
| API keys | GNOME Keyring (`kiko-identity` attribute) |
| Logs | `journalctl --user -u kiko-lite -f` |

---

## Troubleshooting

### Tray icon not appearing

```bash
# Check if running
ps aux | grep kiko-tray

# Check logs
journalctl --user -u kiko-lite -f

# Manual start (debug)
cd ~/.local/share/kiko/gateway
PYTHONPATH=. python3 -m kiko_lite serve --no-auth --port 4000 &
~/.local/bin/kiko-tray
```

### "Master key: NOT FOUND" in doctor

**Normal** when running with `--no-auth` (home LAN mode). The tray starts gateway with `--no-auth`.

To use auth:
```bash
# Store master key
secret-tool store --label="kiko MASTER_KEY" kiko-identity MASTER_KEY

# Restart without --no-auth (edit systemd unit or env)
systemctl --user restart kiko-lite
```

### Keys not found by gateway

**Known issue**: `setup-keys.sh` stores with `service /peterk key NAME username USER`, but `kikolib/keystore.py` reads with `kiko-identity NAME`.

**Workaround** (until fixed):
```bash
# Re-store with correct attribute
secret-tool store --label="kiko NVIDIA_API_KEY" kiko-identity NVIDIA_API_KEY
secret-tool store --label="kiko GEMINI_API_KEY" kiko-identity GEMINI_API_KEY
```

Then `kiko doctor` will show them as `present` in the gateway too.

### Port already in use

```bash
# Check what's on 4000
ss -ltnp | grep 4000

# Change port
# Edit ~/.config/systemd/user/kiko-lite.service:
#   ExecStart=... --port 4001
systemctl --user daemon-reload
systemctl --user restart kiko-lite
```

---

## Uninstall

```bash
kiko uninstall          # Removes service, code, wrapper
kiko uninstall --purge  # Also removes ~/.kiko/kiko.db
```

**Keys in GNOME Keyring are NOT removed** (they're your data).

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        YOUR MACHINE                             │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐    HTTP :4000    ┌────────────────────────┐  │
│  │  kiko-tray   │ ◄──────────────► │    kiko-lite gateway   │  │
│  │  (GTK3/XApp) │   health/poll    │  (Python, stdlib-only) │  │
│  │  ~/.local/   │                  │  ~/.local/share/kiko/  │  │
│  └──────────────┘                  └───────────┬────────────┘  │
│         │                                       │              │
│         │            ┌──────────────────────────┘              │
│         │            ▼                                         │
│         │  ┌─────────────┐    ┌──────────────┐                │
│         └─►│ GNOME Keyring│    │  SQLite DB   │                │
│            │ (API keys)  │    │ (~/.kiko/)   │                │
│            └─────────────┘    └──────────────┘                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────────┐
                    │  AI PROVIDERS       │
                    │  Gemini / NVIDIA /  │
                    │  Mistral / OpenAI / │
                    │  OpenRouter / Groq  │
                    └─────────────────────┘
```

---

## What's Not Perfect (Known Issues)

| Issue | Status | Workaround |
|-------|--------|------------|
| Keystore attribute mismatch | 🔴 Open | Re-store keys with `kiko-identity` attribute |
| `python3.12-venv` needed for tests | 🟡 Doc'd | Install it (in prerequisites above) |
| `GtkStatusIcon` deprecated on non-Cinnamon | 🟡 Known | Works; XApp preferred on Mint |
| No auto schema migration | 🟡 Known | `kiko fix-key` or reinstall |
| `setup-keys.sh` doesn't prompt all keys | 🟡 Minor | Add manually with `secret-tool` |

---

## Next Steps After Install

1. **Test with Claude Code** — set `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`
2. **Add your own models** — `kiko add-model ...` or `python3 -m kiko_lite add-model ...`
3. **Set a monthly cap** — `kiko set-setting monthly_cap_usd 50`
4. **Check costs** — `kiko costs` or tray → Costs dialog
5. **Read the docs** — `docs/LINUX-INSTALL.md`, `docs/GITHUB-DEPLOY.md`

---

## License

MIT — Copyright Peter Kokosharov <peter.kokosharov@gmail.com>

Portions ported from litellm (BerriAI, MIT).