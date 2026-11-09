# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Tests for KIKO auth fail-closed behavior (Task #12).

Covers:
  - All three accepted auth header formats (Bearer, x-api-key, x-goog-api-key)
  - Wrong key → 401
  - Missing key → 401
  - run_server() exits(1) when MASTER_KEY is absent
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_KIKO_ROOT = Path(__file__).parents[2]
if str(_KIKO_ROOT) not in sys.path:
    sys.path.insert(0, str(_KIKO_ROOT))

SECRET = "sk-failclosed-test"


@pytest.fixture
def auth_client(monkeypatch):
    """
    TestClient with MASTER_KEY set in env.
    Auth module is reloaded so _master_key() picks up the env var.
    _cached_app is reset so create_app() runs fresh and imports the
    reloaded user_api_key_auth.
    """
    monkeypatch.setenv("MASTER_KEY", SECRET)
    import importlib

    import litellm.proxy.auth.user_api_key_auth as auth_mod
    importlib.reload(auth_mod)
    import litellm.proxy.kiko_server as ks
    # Reset the cached FastAPI app so create_app() re-imports auth_mod.
    ks._cached_app = None
    yield TestClient(ks.app)
    # Restore cache state for isolation.
    ks._cached_app = None


# ── three accepted header formats ────────────────────────────────────────────

def test_bearer_correct_key_not_401(auth_client):
    """Authorization: Bearer <key> with correct key must not be rejected."""
    r = auth_client.get("/v1/models", headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code != 401


def test_x_api_key_correct_key_not_401(auth_client):
    """x-api-key: <key> with correct key must not be rejected."""
    r = auth_client.get("/v1/models", headers={"x-api-key": SECRET})
    assert r.status_code != 401


def test_x_goog_api_key_correct_key_not_401(auth_client):
    """x-goog-api-key: <key> with correct key must not be rejected."""
    r = auth_client.get("/v1/models", headers={"x-goog-api-key": SECRET})
    assert r.status_code != 401


# ── rejection cases ───────────────────────────────────────────────────────────

def test_wrong_key_401(auth_client):
    """A wrong key via Bearer must return 401."""
    r = auth_client.get("/v1/models", headers={"Authorization": "Bearer wrongkey"})
    assert r.status_code == 401


def test_missing_key_401(auth_client):
    """A request with no auth header at all must return 401."""
    r = auth_client.get("/v1/models")
    assert r.status_code == 401


# ── run_server fail-closed ────────────────────────────────────────────────────

def test_run_server_exits_without_master_key(monkeypatch, tmp_path):
    """run_server() must call sys.exit(1) when MASTER_KEY is absent from both
    the environment and the Credential Manager."""
    monkeypatch.delenv("MASTER_KEY", raising=False)

    import litellm.proxy.kiko_server as ks

    # Credential Manager mock: no key available
    monkeypatch.setattr(ks, "_cred_get", lambda name: "")

    cfg = tmp_path / "kiko.yaml"
    cfg.write_text("model_list: []\n")
    monkeypatch.setattr(sys, "argv", ["kiko", "--config", str(cfg)])

    with pytest.raises(SystemExit) as exc_info:
        ks.run_server()

    assert exc_info.value.code == 1
