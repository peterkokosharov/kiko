# NOSONAR review proposals (SonarQube run 2, 2026-11-26)

Status: proposal only. No NOSONAR marker has been added to the code. An independent reviewer decides each entry.
The NaN idioms (S1764), the `/tmp` literal (S5443) and the S3516 blockers were fixed in code and are not listed here.

All S5332 entries below were opened and checked: none lets a user-chosen remote host be reached over plain http.
Where a remote URL is possible (chat_front.py:87, http.py) https is accepted and passed through unchanged.

## 1. gateway/kikolib/websocket.py:70 - S4790 (weak hash, SHA-1)
Code: `return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")`
Reason: RFC 6455 section 4.2.2 mandates SHA-1 of key+GUID for Sec-WebSocket-Accept. It is a handshake echo, not a security primitive; no alternative hash is protocol-valid.
Suggested: `# NOSONAR - SHA-1 is mandated by RFC 6455 for Sec-WebSocket-Accept; not used for security`

## 2. tools/win/kiko_chain.py:33 - S5332
Code: `GW_BASE = f"http://{HOST}:{GW_PORT}/v1"` (HOST = "127.0.0.1" constant two lines above)
Reason: loopback-only local gateway base; traffic never leaves the machine and there is no certificate to verify.
Suggested: `# NOSONAR - loopback only (127.0.0.1), no TLS needed`

## 3. tools/win/kiko_chain.py:205 - S5332
Code: `with urllib.request.urlopen(f"http://{HOST}:{KIKO_PORT}/health", timeout=3) as r:`
Reason: health probe of the local kiko-lite on 127.0.0.1.
Suggested: `# NOSONAR - loopback health probe`

## 4. tools/win/kiko_chain.py:208 - S5332
Code: `req = urllib.request.Request(f"http://{HOST}:{KIKO_PORT}/v1/models",`
Reason: same loopback host; sends the local master key only to 127.0.0.1.
Suggested: `# NOSONAR - loopback only (127.0.0.1)`

## 5. gateway/kiko_lite/chat_front.py:87 - S5332
Code: `if url.startswith(("http://", "https://")):`
Reason: not a connection; it only classifies a client-supplied image URL and forwards it to the provider unchanged. https is accepted as well; the proxy never fetches the URL itself.
Suggested: `# NOSONAR - scheme check only, https accepted too; URL is forwarded, not fetched here`

## 6. gateway/kikolib/config.py:532 - S5332
Code: `out["anthropic_base_url"] = f"http://{out['proxy_host']}:{out['proxy_port']}"`
Reason: URL shown to local clients for the kiko proxy, which listens on loopback by default; TLS is not terminated by kiko itself.
Suggested: `# NOSONAR - local proxy listener (loopback by default), no TLS termination in kiko`

## 7. gateway/kikolib/http.py:287 - S5332
Code: `u = urlsplit(raw if "://" in raw else f"http://{raw}")`
Reason: only normalises a proxy setting written without a scheme (e.g. "host:3128"); an explicit https:// proxy value is kept as given. It does not touch the target URL.
Suggested: `# NOSONAR - default scheme for a bare proxy setting; explicit scheme is kept`

## 8. gateway/kikolib/http.py:411 - S5332
Code: `req_target = f"http://{u.netloc.rpartition('@')[2]}{target}"`
Reason: absolute-form request line, required by HTTP/1.1 when sending a plain-http request through a forward proxy. Only built for scheme http targets (https goes through CONNECT).
Suggested: `# NOSONAR - absolute-form target mandated for plain-http via proxy; https uses CONNECT`

## 9. gateway/kikolib/server.py:481 - S5332
Code: `and host is not None and h["origin"].strip().lower() == f"http://{host.strip().lower()}")`
Reason: string comparison of the browser Origin header with the server's own Host (CSRF/same-origin guard); no connection is made. The listener is loopback-only and plain http.
Suggested: `# NOSONAR - same-origin string comparison, no connection; listener is loopback http`

## 10. gateway/kikolib/server.py:625 - S5332
Code: `return f"http://{self.host}:{self.port}"`
Reason: URL of the server's own listener, bound to loopback by default.
Suggested: `# NOSONAR - own listener URL, loopback by default`

## 11. gateway/kikolib/websocket.py:461 - S5332
Code: `if origin != f"http://{host.strip().lower()}" and origin not in cfg.origins:`
Reason: same-origin string comparison for the WebSocket upgrade; no connection made.
Suggested: `# NOSONAR - same-origin string comparison, no connection`

## 12. gateway/kikolib/web/security.py:118 - S5332
Code: `return bool(host) and headers["origin"].strip().lower() == f"http://{host.strip().lower()}"`
Reason: same-origin check of the Origin header against Host for the local settings UI; no connection.
Suggested: `# NOSONAR - same-origin string comparison, no connection`

## 13. gateway/kikolib/web/settings.py:78 - S5332
Code: `url = f"http://{kconfig.proxy_host(cfg)}:{kconfig.proxy_port(cfg)}"`
Reason: display URL of the local proxy on the settings page; loopback by default.
Suggested: `# NOSONAR - display of local proxy URL, loopback by default`

## 14. tray/tk/tests/test_model.py:43 - S5332
Code: `self.assertIn("not reachable", model.tooltip("down", None, "http://h:4000", "down"))`
Reason: string literal in a unit test; the function only formats text, no network access.
Suggested: `# NOSONAR - test literal, no network`
