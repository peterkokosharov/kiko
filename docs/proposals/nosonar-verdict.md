# NOSONAR review by nemotron-ultra

gateway/kikolib/websocket.py:70 ACCEPT - SHA-1 is mandated by RFC 6455 for Sec-WebSocket-Accept handshake; not a security primitive.
tools/win/kiko_chain.py:33 ACCEPT - Hardcoded 127.0.0.1 loopback only; traffic never leaves host, no TLS needed.
tools/win/kiko_chain.py:205 ACCEPT - Loopback health probe to 127.0.0.1; no external exposure.
tools/win/kiko_chain.py:208 ACCEPT - Loopback request to 127.0.0.1; master key never leaves machine.
gateway/kiko_lite/chat_front.py:87 ACCEPT - Scheme classification only; URL forwarded unchanged, not fetched; https accepted.
gateway/kikolib/config.py:532 ACCEPT - Local proxy listener URL (loopback default); kiko does not terminate TLS.
gateway/kikolib/http.py:287 ACCEPT - Defaults bare proxy setting to http; explicit https:// preserved; target URL untouched.
gateway/kikolib/http.py:411 ACCEPT - Absolute-form required by HTTP/1.1 for plain-http via forward proxy; https uses CONNECT.
gateway/kikolib/server.py:481 ACCEPT - Origin vs Host string comparison for CSRF guard; no connection; listener loopback http.
gateway/kikolib/server.py:625 ACCEPT - Server's own listener URL; bound to loopback by default.
gateway/kikolib/websocket.py:461 ACCEPT - Same-origin string check for WebSocket upgrade; no network connection.
web/security.py:118 ACCEPT - Origin vs Host string comparison for local UI; no connection.
web/settings.py:78 ACCEPT - Display string for local proxy URL; loopback by default.
tray/tk/tests/test_model.py:43 ACCEPT - Test string literal; function formats text only, no network access.
SUMMARY: 14 ACCEPT, 0 REJECT
