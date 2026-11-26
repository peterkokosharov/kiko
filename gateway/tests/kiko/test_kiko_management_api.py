# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The tray's /kiko/v1/* contract: token, exact reply shapes, names-only keys, redacted logs."""
import http.client
import json
import os

import pytest

from kiko_lite import keys, management
from kiko_lite.db import Database
from kiko_lite.resolver import build_db_app
from kikolib import server

TOKEN = "management-token-1234567890"
JSON = {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN}


def _get(srv, path, headers=None, method="GET", body=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    try:
        c.request(method, path, body=body, headers=headers if headers is not None else JSON)
        r = c.getresponse()
        return r.status, json.loads(r.read())
    finally:
        c.close()


@pytest.fixture
def srv(tmp_path):
    db = Database(tmp_path / "kiko.db")
    db.upsert_model("fast", "gemini/gemini-flash-lite-latest", "GEMINI_API_KEY",
                    model_class="HAIKU", status="ok")
    db.upsert_model("slow", "nvidia/mistral-large-4-1t-a52b", "NVIDIA_API_KEY",
                    model_class="SONNET", status="error", enabled=False)
    db.add_billing("fast", 6, 1, 0.5, route="messages")
    app = build_db_app(db, None, secret=lambda n: None, auth=False)
    api = management.install(app, db, tmp_path / "kiko.db", 4000, token=TOKEN)
    s = server.Server(app, port=0).start()
    yield s, api
    s.stop()


def test_management_refuses_requests_without_the_token(srv):
    s, _ = srv
    for path in ("/kiko/v1/status", "/kiko/v1/models", "/kiko/v1/keys", "/kiko/v1/logs"):
        status, body = _get(s, path, headers={})
        assert status == 401, (path, status)
        assert body["error"]["code"] == "unauthorized", body


def test_status_has_api_version_uptime_pid_and_enabled_model_count(srv):
    s, api = srv
    status, body = _get(s, "/kiko/v1/status")
    assert status == 200, body
    assert body["api_version"] == 1
    assert body["uptime_s"] >= 0
    assert body["pid"] > 0
    assert body["models_enabled"] == 1
    assert body["db_path"].endswith("kiko.db")


def test_models_and_classes_match_the_contract_shapes(srv):
    s, _ = srv
    status, body = _get(s, "/kiko/v1/models")
    assert status == 200, body
    # Database order is model_class, priority, id: HAIKU before SONNET.
    assert body == {"models": [
        {"name": "fast", "class": "HAIKU", "enabled": True, "status": "ok",
         "context_window": None, "max_output": None},
        {"name": "slow", "class": "SONNET", "enabled": False, "status": "error",
         "context_window": None, "max_output": None},
    ]}
    status, body = _get(s, "/kiko/v1/classes")
    assert status == 200, body
    assert any(c["name"] == "HAIKU" and c["keyword"] == "haiku" for c in body["classes"])
    assert all(set(c) == {"name", "keyword", "role"} for c in body["classes"])


def test_keys_expose_names_and_presence_but_never_a_value(srv, monkeypatch):
    s, _ = srv
    monkeypatch.setenv("GEMINI_API_KEY", "value-must-never-be-sent")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)  # the host may have a real one exported
    monkeypatch.setattr(keys.keystore, "get_secret", lambda *a, **k: None)  # nor a real one in the OS key store
    status, body = _get(s, "/kiko/v1/keys")
    assert status == 200, body
    by_name = {k["name"]: k["present"] for k in body["keys"]}
    assert by_name["GEMINI_API_KEY"] is True
    assert by_name["NVIDIA_API_KEY"] is False
    assert "value-must-never-be-sent" not in json.dumps(body)
    assert all(set(k) == {"name", "present"} for k in body["keys"])


def test_costs_report_today_period_cap_and_per_model(srv):
    s, _ = srv
    status, body = _get(s, "/kiko/v1/costs?days=30")
    assert status == 200, body
    assert body["days"] == 30
    assert body["today_usd"] == pytest.approx(0.5)
    assert body["period_usd"] == pytest.approx(0.5)
    assert body["cap_usd"] is None
    assert body["per_model"] == [{"model": "fast", "usd": pytest.approx(0.5),
                                  "tokens_in": 6, "tokens_out": 1}]


def test_logs_are_redacted_before_they_leave_the_server(srv, monkeypatch):
    s, api = srv
    monkeypatch.setattr(management.kconfig, "log_file", lambda: None)
    secret = "sk-live-SECRETVALUE123456789"
    api.db.add_billing("fast", 1, 1, 0.0, error="upstream key=" + secret)
    status, body = _get(s, "/kiko/v1/logs?lines=10")
    assert status == 200, body
    assert body["lines"], body
    assert secret not in json.dumps(body)
    assert "***REDACTED***" in json.dumps(body)


def test_set_class_is_visible_to_the_next_request_and_validates_input(srv):
    s, _ = srv
    status, body = _get(s, "/kiko/v1/models/fast/class", method="POST",
                        body=json.dumps({"class": "OPUS"}))
    assert status == 200, body
    assert body == {"name": "fast", "class": "OPUS"}
    status, body = _get(s, "/kiko/v1/models")
    assert status == 200, body
    assert {"name": "fast", "class": "OPUS", "enabled": True, "status": "ok",
            "context_window": None, "max_output": None} in body["models"]
    status, body = _get(s, "/kiko/v1/models/fast/class", method="POST",
                        body=json.dumps({"class": "NOPE"}))
    assert status == 400 and body["error"]["code"] == "bad_request", body
    status, body = _get(s, "/kiko/v1/models/missing/class", method="POST",
                        body=json.dumps({"class": "OPUS"}))
    assert status == 404 and body["error"]["code"] == "not_found", body


def test_token_file_is_created_private_and_read_back(tmp_path, monkeypatch):
    monkeypatch.delenv("KIKO_API_TOKEN", raising=False)
    first = management.token_for(tmp_path / "kiko.db")
    path = tmp_path / "api-token"
    assert len(first) >= 64
    assert path.read_text().splitlines()[0] == first
    if os.name == "posix":  # Windows has no POSIX file modes: os.open(0o600) is ignored there (the profile ACL applies)
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert management.token_for(tmp_path / "kiko.db") == first
