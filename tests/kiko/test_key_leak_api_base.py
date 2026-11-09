# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Security test: routing key-leak via api_base injection (S1).

Suspected vulnerability: a client sends model="gemini/x" (NOT in model_list)
together with api_base pointing to an attacker-controlled URL.  If kiko naively
forwards the request, the attacker receives the master key / user key.

Expected correct behaviour: kiko rejects with 404 (unknown model) and never
contacts the attacker URL.

Run standalone:
    cd <kiko-root>
    .venv/Scripts/python -m pytest tests/kiko/test_key_leak_api_base.py -v
"""
import json
import os
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Mock attacker HTTP server (stdlib only)
# ---------------------------------------------------------------------------

class _RecordingHandler(BaseHTTPRequestHandler):
    """Counts every inbound request; never returns anything meaningful."""

    def _handle(self):
        self.server.requests_received += 1
        content_len = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(content_len)
        self.send_response(200)
        self.end_headers()

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle

    def log_message(self, *_args):
        pass  # silence default stderr output


def _start_mock_server() -> HTTPServer:
    """Bind to an OS-assigned free port; start serving in a daemon thread."""
    httpd = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    httpd.requests_received = 0
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


# ---------------------------------------------------------------------------
# Kiko fixture — minimal config, no real API keys required
# ---------------------------------------------------------------------------

_SECURITY_PORT = 14009
_MASTER_KEY = "sk-kiko-sec-test"


def _wait_health(port: int, timeout: int = 45) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(
                f"http://localhost:{port}/health/liveliness", timeout=2
            )
            return True
        except Exception:
            time.sleep(1)
    return False


@pytest.fixture(scope="module")
def kiko_security_proxy(tmp_path_factory):
    """Spin up kiko with a one-model config that uses a fake/unreachable key.

    We never actually call the upstream — the test only checks that requests
    for *unknown* models are rejected before kiko touches any network.
    """
    tmp = tmp_path_factory.mktemp("kiko_security")
    conf_path = tmp / "kiko-security.yaml"
    db_path = tmp / "kircho.db"

    # One known model with a fake key; "gemini/x" is intentionally absent.
    conf_path.write_text(
        """
model_list:
  - model_name: known-model
    litellm_params:
      model: openai/gpt-fake
      api_key: sk-fake-not-used
      api_base: http://127.0.0.1:1/nonexistent

litellm_settings:
  drop_params: true
"""
    )

    _scripts = Path(sys.executable).parent
    kiko_bin = _scripts / ("kiko.EXE" if sys.platform == "win32" else "kiko")
    if not kiko_bin.exists():
        pytest.skip("kiko binary not found — run: uv pip install -e '.[proxy]'")

    env = os.environ.copy()
    env["MASTER_KEY"] = _MASTER_KEY
    env["KIKO_DB_PATH"] = str(db_path)

    proc = subprocess.Popen(
        [str(kiko_bin), "--config", str(conf_path), "--port", str(_SECURITY_PORT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not _wait_health(_SECURITY_PORT):
        proc.terminate()
        pytest.fail("kiko (security fixture) did not become healthy within 45 s")

    yield {"url": f"http://localhost:{_SECURITY_PORT}", "key": _MASTER_KEY}

    proc.terminate()
    proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# S1 — api_base injection with unknown model must be rejected (404)
# ---------------------------------------------------------------------------

def test_api_base_injection_unknown_model(kiko_security_proxy):
    """
    Send model="gemini/x" (not in model_list) with api_base → mock server.

    Pass:  HTTP 404 returned; mock server request count == 0.
    Fail:  Any other status code, OR mock server count > 0 (leak confirmed).
    """
    mock = _start_mock_server()
    attacker_url = f"http://127.0.0.1:{mock.server_address[1]}"

    body = json.dumps(
        {
            "model": "gemini/x",
            "messages": [{"role": "user", "content": "hi"}],
            "api_base": attacker_url,
            "stream": False,
        }
    ).encode()

    req = urllib.request.Request(
        f"{kiko_security_proxy['url']}/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {kiko_security_proxy['key']}",
            "Content-Type": "application/json",
        },
    )

    status_code: int | None = None
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            status_code = r.status
    except urllib.error.HTTPError as exc:
        status_code = exc.code

    # Wait a moment so any stray async forward request can arrive at mock.
    time.sleep(1)
    mock.shutdown()

    requests_at_mock = mock.requests_received

    # Collect both facts before asserting, so the failure message is complete.
    leak_detected = requests_at_mock > 0
    wrong_status = status_code != 404

    failure_parts = []
    if wrong_status:
        failure_parts.append(
            f"Expected HTTP 404 (unknown model), got {status_code}. "
            "kiko may have routed 'gemini/x' instead of rejecting it."
        )
    if leak_detected:
        failure_parts.append(
            f"KEY LEAK CONFIRMED: mock attacker server received "
            f"{requests_at_mock} request(s). "
            "kiko forwarded the request to the api_base URL supplied by the client."
        )

    assert not failure_parts, " | ".join(failure_parts)


# ---------------------------------------------------------------------------
# S2 — api_base injection with a KNOWN model must also be blocked
# ---------------------------------------------------------------------------

def test_api_base_injection_known_model(kiko_security_proxy):
    """
    Send model="known-model" (a valid alias) together with api_base pointing
    to a mock attacker server.

    _prepare() must strip the client-supplied api_base and use the configured
    one (http://127.0.0.1:1/nonexistent) so the evil mock receives 0 requests.
    The upstream call will fail (fake host), but that is irrelevant here.

    Pass:  mock server receives 0 requests.
    Fail:  mock server receives ≥ 1 request (kiko forwarded to evil URL).
    """
    mock = _start_mock_server()
    attacker_url = f"http://127.0.0.1:{mock.server_address[1]}"

    body = json.dumps(
        {
            "model": "known-model",
            "messages": [{"role": "user", "content": "hi"}],
            "api_base": attacker_url,   # evil: should be stripped by _prepare()
            "stream": False,
        }
    ).encode()

    req = urllib.request.Request(
        f"{kiko_security_proxy['url']}/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {kiko_security_proxy['key']}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as _r:
            pass  # unlikely with the fake configured host; fine if it succeeds
    except urllib.error.HTTPError:
        pass  # 4xx/5xx expected — configured upstream is unreachable
    except Exception:
        pass  # network-level errors also acceptable

    # Allow time for any stray async forward to arrive at mock.
    time.sleep(1)
    mock.shutdown()

    assert mock.requests_received == 0, (
        f"KEY LEAK: evil mock server received {mock.requests_received} request(s). "
        "kiko used the client-supplied api_base instead of the configured one."
    )
