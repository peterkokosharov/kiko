# kikolib changes

## 0.2.1 - lingering close reads up to 1 MiB

`server.LINGER_BYTES` 64 KiB -> 1 MiB. A request rejected early (403/415/411) with a body larger than the cap
was closed with unread data, and on macOS (smaller loopback buffers) that RST destroyed the answer before the
client read it. The close is still bounded in time (1 s) and runs per connection thread.

## 0.2.0 - first pinned release

First release meant to be copied unmodified into other projects (see README, "How other projects take
kikolib"). Ships `SHA256SUMS`. Version 0.1.0 was an unreleased stamp and is ambiguous; do not use it.

Modules: config, dotenv, factory, http, keystore, logs, posix, retry, server, sse, tokens, tomlwrite,
websocket, winhttp, yaml_lite.
