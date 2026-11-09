# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus, Anthropic Sonnet
"""
Regression tests for Fable audit H1: MASTER_KEY available only via Credential Manager.
run_server() copies the Credential Manager key into os.environ after the auth module is
imported; auth must read the key per request, not freeze it at import.
No real Credential Manager, network or server is used (_cred_get and uvicorn.run are patched).
"""
import sys
import warnings
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

_KIKO_ROOT = Path(__file__).parents[2]
if str(_KIKO_ROOT) not in sys.path:
    sys.path.insert(0, str(_KIKO_ROOT))

SECRET = "secret"


@pytest.fixture
def server(monkeypatch):
    """kiko_server imported with no MASTER_KEY in env; Credential Manager mocked."""
    monkeypatch.delenv("MASTER_KEY", raising=False)
    import importlib

    import litellm.proxy.auth.user_api_key_auth as auth_mod
    importlib.reload(auth_mod)  # import-time state with no key in env
    import litellm.proxy.kiko_server as ks
    monkeypatch.setattr(ks, "user_api_key_auth", auth_mod.user_api_key_auth, raising=False)
    monkeypatch.setattr(ks, "_cred_get", lambda name: SECRET if name == "MASTER_KEY" else "")
    monkeypatch.setattr(ks, "_config_path", ks._config_path)  # restored after run_server sets it
    return ks, auth_mod


def _run_server(ks, monkeypatch, tmp_path):
    import uvicorn
    cfg = tmp_path / "kiko.yaml"
    cfg.write_text("model_list: []\n")
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(sys, "argv", ["kiko", "--config", str(cfg)])
    ks.run_server()
    assert len(calls) == 1, "run_server did not reach uvicorn.run"


def test_validate_token_rejects_bad_token_after_credman_fallback(server, monkeypatch, tmp_path):
    ks, auth_mod = server
    _run_server(ks, monkeypatch, tmp_path)
    import os
    assert os.environ.get("MASTER_KEY") == SECRET  # set by the real fallback code
    with pytest.raises(HTTPException) as ei:
        auth_mod._validate_token("bad")
    assert ei.value.status_code == 401
    assert auth_mod._validate_token(SECRET).api_key == SECRET


def test_http_wrong_token_401_correct_token_accepted(server, monkeypatch, tmp_path):
    ks, _ = server
    _run_server(ks, monkeypatch, tmp_path)
    client = TestClient(ks.app)  # no context manager: lifespan not run
    r = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = client.get("/v1/models", headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code != 401
    r = client.get("/v1/models")  # missing header
    assert r.status_code == 401


def test_frozen_empty_key_now_rejects_all_requests(server, monkeypatch, tmp_path):
    """Fail-closed: a key frozen at import (empty) now correctly rejects every request.
    Previously the server would accept any non-empty token when the frozen key was empty
    (the old bug); after the fail-closed fix it always returns 401 in that scenario."""
    ks, auth_mod = server
    frozen = ""  # what os.environ.get("MASTER_KEY", "") returned at import time
    monkeypatch.setattr(auth_mod, "_master_key", lambda: frozen)
    _run_server(ks, monkeypatch, tmp_path)
    client = TestClient(ks.app)
    r = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401  # fail-closed: empty frozen key rejects all requests
