# kiko embedded

kiko embedded, binary `kiko-wrt`: a small OpenAI-compatible LLM proxy written in C for OpenWrt routers (and plain Linux).
Plain C (C11), final; C++ was considered and rejected (libstdc++ not on default images, size).

It is the little sibling of **kiko**, a Python LiteLLM-style gateway. It runs on the home router,
keeps the provider API keys there, and lets LAN clients (rex, Layka, Sisi, Lassie, opencode, ...) talk
OpenAI Chat Completions to one local endpoint. It forwards to:

- OpenAI and any OpenAI-compatible upstream (passthrough), and
- Google Gemini (request/response translation),

with SSE streaming both ways, tool/function calling, `/v1/models`, a LAN client token, per-model routing
from UCI (`/etc/config/kiko-wrt`), a procd init script and syslog logging.

Targets: stripped binary < 300 KB, < 8 MB RAM idle, OpenWrt-native libraries only, packaged as `.ipk`
(OpenWrt 24.10) and `.apk` (OpenWrt 25.x); also builds with CMake on desktop Linux for development.

## libkiko

kiko-wrt is built on **libkiko** (`libkiko/`), a static C library that is the C sibling of **kikolib**
(`../gateway/kikolib/`, Python). libkiko ports kikolib's HTTP client and server, SSE, retry, keystore,
posix helpers and token estimate to C on OpenWrt's event loop, with the same names where sensible
(`kikolib.sse.Parser.feed` -> `kiko_sse_parser_feed`). kikolib stays the single source for Python
projects; libkiko is the single source for C projects. Neither copies code from the other; when one
changes behaviour, the other is checked against it. Size budget: libkiko <= 80 KB of the 300 KB.
The keystore comes from kiko-identity (C).

## Status

**Plan only.** No code yet. Phase 0 (measuring the real kiko at home) comes first, then Phase 1 (libkiko),
Phase 2 (the kiko-wrt proxy on libkiko) and Phase 3 (hardening, packaging, release).

## Documents

- [docs/SPEC.md](docs/SPEC.md) - endpoints, OpenAI <-> Gemini mapping, config, security, non-goals
- [docs/PLAN.md](docs/PLAN.md) - kikolib -> libkiko module decisions, phases, "done when", agent-day
  estimates, parallel lanes, risks
- [docs/TEAM.md](docs/TEAM.md) - roles, review rules, C safety checklist
- [docs/TESTING.md](docs/TESTING.md) - unit, mock upstream, contract, fuzzing, Docker/OpenWrt, real router

## License

MIT - Copyright Peter Kokosharov <peter.kokosharov@gmail.com>, see [LICENSE](../LICENSE) in the repository root.
