# kiko-lite

kiko-lite is a small home model router with no third-party dependencies (Python standard library only). It speaks
the Anthropic Messages API, so Claude Code can use it as its backend. Claude Code asks for a model CLASS
(OPUS, SONNET, HAIKU, FABLE) and kiko-lite answers with an alive model of that class from Gemini, NVIDIA, Mistral,
OpenAI-compatible providers and others. It fails over to the next model when one is down, puts failing models on
a cooldown, records usage and cost in SQLite, and enforces a monthly spending cap.

## Requirements

- Python 3.11 or newer (`kikolib` refuses to import on older versions).
- Linux (Debian 12, Linux Mint, Q4OS) or Windows. No pip install is needed.
- An API key for at least one provider.

## Quick start

Linux:

    chmod +x kiko-lite
    ./kiko-lite add-model gemini-flash --provider gemini/gemini-3.8-flash --api-key-env GEMINI_API_KEY --class OPUS
    GEMINI_API_KEY=your-key MASTER_KEY=choose-a-secret ./kiko-lite serve

Windows (Command Prompt):

    kiko-lite.cmd add-model gemini-flash --provider gemini/gemini-3.8-flash --api-key-env GEMINI_API_KEY --class OPUS
    set GEMINI_API_KEY=your-key
    set MASTER_KEY=choose-a-secret
    kiko-lite.cmd serve

Then point Claude Code at `http://127.0.0.1:4000` (`ANTHROPIC_BASE_URL`, and MASTER_KEY as `ANTHROPIC_AUTH_TOKEN`; see docs/LINUX-INSTALL.md). Run
`kiko-lite --help` (Windows: `kiko-lite.cmd --help`) for all commands and `kiko-lite doctor` (Windows:
`kiko-lite.cmd doctor`) to check the installation. Keys can also be kept in the
OS key store instead of environment variables.

To run it in the background on Linux (Mint, Q4OS), use `./install-service.sh` (systemd user service; see section 10
of docs/LINUX-INSTALL.md).

More: [docs/LINUX-INSTALL.md](docs/LINUX-INSTALL.md) (step by step on Linux),
[docs/GITHUB-DEPLOY.md](docs/GITHUB-DEPLOY.md) (deploying and updating from GitHub).

## Tests

The zip bundles the standalone unit tests in `tests/kiko/`. They need only pytest (not part of the zip):
`python -m pytest tests/kiko`, run from the unpacked folder. Tests that need repo-only sample files are skipped.

## Security

- The server binds to 127.0.0.1 by default. Do not expose it to a network you do not trust; `--no-auth` is for a
  trusted home LAN only.
- Requests that come from a web browser are refused.
- API keys are never written to the database or to logs.

## Version and licence

The build is named in the `VERSION` file. MIT License, see `LICENSE`. Portions are ported from litellm
(BerriAI, MIT).
