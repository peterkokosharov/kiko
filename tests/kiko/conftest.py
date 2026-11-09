# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet, Anthropic Opus
"""
Fixtures for KIKO integration tests.
Starts kiko against a temp DB and Gemini only config.
Requires: KIRCHO/GEMINI_API_KEY in Windows Credential Manager.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import atexit
import shutil

import pytest

# ---------------------------------------------------------------------------
# Home isolation: runs at conftest import, before any kiko module is imported,
# so no test can resolve the real ~/.kiko/kiko.db (TASK #86).
# ---------------------------------------------------------------------------
_ISOLATED_HOME = Path(tempfile.mkdtemp(prefix="kiko_test_home_"))
atexit.register(shutil.rmtree, _ISOLATED_HOME, True)
_ISOLATED_DB = _ISOLATED_HOME / ".kiko" / "kiko.db"
_ISOLATED_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(_ISOLATED_HOME)
os.environ["USERPROFILE"] = str(_ISOLATED_HOME)
os.environ["KIKO_DB_PATH"] = str(_ISOLATED_DB)


@pytest.fixture(autouse=True)
def _isolate_default_db(monkeypatch):
    """Redirect the import-time _DEFAULT_DB of the sqlite logger to tmp."""
    mod = sys.modules.get("litellm.proxy.integrations.kiko_sqlite_logger")
    if mod is not None and hasattr(mod, "_DEFAULT_DB"):
        monkeypatch.setattr(mod, "_DEFAULT_DB", _ISOLATED_DB)


@pytest.fixture(autouse=True)
def _isolate_config_dir(monkeypatch, tmp_path_factory):
    """Every test gets its own empty KIKO_CONFIG_DIR (kiko.toml, .env, logs): none reads or writes a real one (TASK #124).
    A test that needs a particular folder sets the variable itself."""
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path_factory.mktemp("kiko_cfg")))


def get_secret(namespace: str, key: str) -> str:
    """Stub: read secrets from environment variables only."""
    return os.environ.get(key, "")

_GEMINI_KEY = get_secret("KIKO", "GEMINI_API_KEY")

PROXY_PORT = 14001  # separate port to avoid conflict with dev proxy on 4000
MASTER_KEY  = "sk-kiko-test"


def _wait_health(port: int, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://localhost:{port}/health/liveliness", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


@pytest.fixture(scope="session")
def kiko_proxy(tmp_path_factory):
    if not _GEMINI_KEY:
        pytest.skip("GEMINI_API_KEY not found in credential store")

    tmp = tmp_path_factory.mktemp("kiko_integration")
    db_path   = tmp / "kircho.db"
    conf_path = tmp / "kiko-models.yaml"

    # Init DB schema (billing + models tables)
    from litellm.proxy.integrations.kiko_sqlite_logger import _init_db
    _init_db(db_path)

    # Write config — NO secrets in this file.
    # Keys are passed exclusively via environment variables (Rule 1).
    # litellm resolves "os.environ/VAR" at runtime from the subprocess env.
    conf_path.write_text("""
model_list:
  - model_name: gemini-flash-lite
    litellm_params:
      model: gemini/gemini-3.5-flash-lite
      api_key: os.environ/KIKO_TEST_GEMINI_KEY

litellm_settings:
  drop_params: true
""")

    env = os.environ.copy()
    env["KIKO_DB_PATH"] = str(db_path)
    env["MASTER_KEY"] = MASTER_KEY
    env["KIKO_TEST_GEMINI_KEY"] = _GEMINI_KEY  # injected into subprocess only, never written to disk

    _scripts = Path(sys.executable).parent
    kiko_bin = _scripts / ("kiko.EXE" if sys.platform == "win32" else "kiko")
    if not kiko_bin.exists():
        pytest.skip("kiko not installed — run: uv pip install -e '.[proxy]' in kiko/")

    proc = subprocess.Popen(
        [str(kiko_bin), "--config", str(conf_path), "--port", str(PROXY_PORT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not _wait_health(PROXY_PORT, timeout=45):
        proc.terminate()
        pytest.fail("kiko did not start within 45 seconds")

    yield {"url": f"http://localhost:{PROXY_PORT}", "db": db_path, "key": MASTER_KEY}

    proc.terminate()
    proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# mock_backend — offline respx fixture (no real API calls)
# ---------------------------------------------------------------------------

_MOCK_UPSTREAM = "http://mock-llm-api/v1"

_NON_STREAM_RESP = {
    "id": "chatcmpl-mock001",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from mock!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

_STREAM_SSE = (
    'data: {"id":"chatcmpl-s01","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-s01","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-s01","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)

# OpenAI-format streaming with text + tool_call — used by /v1/messages streaming tests.
# litellm's AnthropicStreamWrapper transforms these chunks into Anthropic SSE events
# (message_start → content_block_start/delta/stop × 2 → message_delta → message_stop).
_STREAM_SSE_WITH_TOOL = (
    'data: {"id":"chatcmpl-s02","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-s02","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_001","type":"function",'
    '"function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-s02","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":"}}]},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-s02","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":" \\"Sofia\\"}"}}]},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-s02","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    "data: [DONE]\n\n"
)

# OpenAI-chat SSE for /v1/responses streaming tests (Step 11).
# aresponses(use_chat_completions_api=True, stream=True) translates the
# Responses API request to chat/completions; the mock upstream returns this
# plain text-only SSE which LiteLLMCompletionStreamingIterator converts into
# Responses API events (response.created → … → response.completed).
_STREAM_SSE_RESPONSES = (
    'data: {"id":"chatcmpl-r01","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-r01","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-r01","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)

# OpenAI-format SSE for Gemini streamGenerateContent tests.
# Includes: text chunk + tool_call chunk (complete args) + final chunk with
# finish_reason + usage in the same JSON object.
# Dispatched when stream_options.include_usage=True in the request body.
_STREAM_SSE_GEMINI = (
    'data: {"id":"chatcmpl-g01","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-g01","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_001","type":"function",'
    '"function":{"name":"get_weather","arguments":"{\\"city\\":\\"Sofia\\"}"}}]},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-g01","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":20,"completion_tokens":15,"total_tokens":35}}\n\n'
    "data: [DONE]\n\n"
)

_TOOL_CALL_RESP = {
    "id": "chatcmpl-mock003",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_mock001",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city":"Sofia"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
}

# Gemini generateContent format (for mock Gemini route)
_GEMINI_RESP = {
    "candidates": [
        {
            "content": {"parts": [{"text": "Mock gemini response"}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
}


@pytest.fixture
def mock_backend(monkeypatch, tmp_path):
    """
    In-process kiko app with respx mocking the upstream LLM HTTP API.
    No real network calls or API keys required.

    The mock upstream at _MOCK_UPSTREAM dispatches by request body:
    - tools present  → tool_calls response
    - stream=true    → SSE text/event-stream response
    - otherwise      → standard JSON chat completion
    A Gemini-format generateContent route is also registered for completeness.

    Yields: {"app": FastAPI app, "db": Path, "key": str}
    Tests should create their own::

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=data["app"]),
            base_url="http://test",
        ) as client:
            ...
    """
    import importlib  # noqa: PLC0415 — intentional lazy import in fixture
    import httpx
    import respx
    import litellm
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import KikoSQLiteLogger, _init_db

    # Isolated SQLite DB
    db_path = tmp_path / "mock_test.db"
    _init_db(db_path)

    monkeypatch.setenv("MASTER_KEY", "sk-mock-test")
    monkeypatch.setenv("KIKO_DB_PATH", str(db_path))
    # Defensive: clear any _billing_db_path leaked by config-loader tests so that
    # _check_monthly_cap falls through to _db_path() → KIKO_DB_PATH env var above.
    monkeypatch.setattr(ks, "_billing_db_path", None)

    # Model list pointing to the respx-intercepted upstream
    monkeypatch.setattr(
        ks,
        "_model_list",
        [
            {
                "model_name": "mock-model",
                "litellm_params": {
                    "model": "openai/gpt-4o-mini",
                    "api_key": "sk-mock-key",
                    "api_base": _MOCK_UPSTREAM,
                },
            },
            # Used by messages (Anthropic Messages API) tests — routed via the
            # chat/completions adapter (use_chat_completions_url_for_anthropic_messages=True).
            {
                "model_name": "mock-claude",
                "litellm_params": {
                    "model": "openai/gpt-4o-mini",
                    "api_key": "sk-mock-key",
                    "api_base": _MOCK_UPSTREAM,
                },
            },
            # Used by Gemini generateContent / countTokens tests.
            # The google_genai adapter translates Gemini↔OpenAI; the mock upstream
            # at _MOCK_UPSTREAM intercepts the resulting chat/completions call.
            {
                "model_name": "mock-gemini",
                "litellm_params": {
                    "model": "openai/gpt-4o-mini",
                    "api_key": "sk-mock-key",
                    "api_base": _MOCK_UPSTREAM,
                },
            },
        ],
    )

    # Route Anthropic-Messages-over-non-Anthropic-backend through chat/completions
    # instead of the Responses API.  The lifespan sets this for the real server,
    # but the ASGI test transport does not call the lifespan, so we set it here.
    _orig_use_chat = litellm.use_chat_completions_url_for_anthropic_messages
    litellm.use_chat_completions_url_for_anthropic_messages = True

    # Register billing logger pointing to the isolated DB.
    #
    # Callback state isolation: save and replace ALL litellm callback lists so that
    # test-order contamination from earlier tests (e.g. 'langfuse' left in
    # success_callback by test_other_callbacks_passed_through) cannot affect this
    # fixture.  Restoring all lists in teardown keeps the session clean for tests
    # that run after us.
    #
    # Special care for litellm._async_success_callback: LoggingCallbackManager
    # _add_custom_logger_to_list deduplicates by class type, so a KikoSQLiteLogger
    # from a previous test would block a new instance from being added.  We replace
    # existing KikoSQLiteLogger entries with our per-test instance directly.
    logger = KikoSQLiteLogger(db_path=db_path)
    _original_callbacks = list(litellm.callbacks)
    _original_success_callback = list(litellm.success_callback)
    _original_failure_callback = list(litellm.failure_callback)
    _original_input_callback = list(litellm.input_callback)
    _original_async_success = list(litellm._async_success_callback)
    _original_async_failure = list(litellm._async_failure_callback)
    litellm.callbacks = [logger]
    litellm.success_callback = []
    litellm.failure_callback = []
    litellm.input_callback = []
    litellm._async_success_callback[:] = [
        cb for cb in _original_async_success if not isinstance(cb, KikoSQLiteLogger)
    ] + [logger]
    litellm._async_failure_callback[:] = [
        cb for cb in _original_async_failure if not isinstance(cb, KikoSQLiteLogger)
    ]

    # Fresh app — clear cache so route handlers pick up current _model_list
    ks._cached_app = None
    app = ks.app

    # Capture upstream request bodies so tests can inspect what was sent.
    _captured_upstream: list[dict] = []

    # Smart upstream handler — dispatches by request body
    def _upstream_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        _captured_upstream.append(body)
        _include_usage = body.get("stream_options", {}).get("include_usage", False)
        if body.get("tools") and body.get("stream"):
            # Streaming with tool_use: return OpenAI SSE with tool_call chunks.
            # litellm transforms these into Anthropic SSE events for /v1/messages.
            return httpx.Response(
                200,
                content=_STREAM_SSE_WITH_TOOL.encode(),
                headers={"content-type": "text/event-stream"},
            )
        if body.get("tools"):
            return httpx.Response(200, json=_TOOL_CALL_RESP)
        if body.get("stream") and _include_usage:
            # Gemini streamGenerateContent: text + tool_call + final chunk with usage.
            return httpx.Response(
                200,
                content=_STREAM_SSE_GEMINI.encode(),
                headers={"content-type": "text/event-stream"},
            )
        if body.get("stream"):
            return httpx.Response(
                200,
                content=_STREAM_SSE.encode(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=_NON_STREAM_RESP)

    with respx.mock(assert_all_called=False, using="httpx") as rx:
        # Intercept the fake upstream LLM API (uses httpx-level patching)
        rx.post(f"{_MOCK_UPSTREAM}/chat/completions").mock(side_effect=_upstream_handler)
        # Gemini route — registered for completeness; generateContent format
        rx.post(url__startswith="http://mock-gemini-api/").mock(
            return_value=httpx.Response(200, json=_GEMINI_RESP)
        )
        # Pass ASGI test-client requests through to the in-process app
        rx.route(url__startswith="http://test/").pass_through()

        yield {"app": app, "db": db_path, "key": "sk-mock-test", "captured": _captured_upstream}

    # Restore global state
    litellm.use_chat_completions_url_for_anthropic_messages = _orig_use_chat
    litellm.callbacks = _original_callbacks
    litellm.success_callback = _original_success_callback
    litellm.failure_callback = _original_failure_callback
    litellm.input_callback = _original_input_callback
    litellm._async_success_callback[:] = _original_async_success
    litellm._async_failure_callback[:] = _original_async_failure
    ks._cached_app = None


# ---------------------------------------------------------------------------
# PRI4 fixtures — lightweight setup for test_pri4_stream_then_chain
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db_path(monkeypatch, tmp_path):
    """Create a temp SQLite DB and set KIKO_DB_PATH for PRI4 test."""
    from litellm.proxy.integrations.kiko_sqlite_logger import _init_db
    db = tmp_path / "pri4_test.db"
    _init_db(db)
    monkeypatch.setenv("KIKO_DB_PATH", str(db))
    return db


@pytest.fixture
def mock_litellm_key(monkeypatch, mock_db_path):
    """Set MASTER_KEY and minimal _model_list (claude-haiku-4-5) for PRI4 test."""
    import litellm.proxy.kiko_server as ks

    monkeypatch.setenv("MASTER_KEY", "sk-test")
    monkeypatch.setattr(ks, "_billing_db_path", None)
    monkeypatch.setattr(ks, "_model_list", [
        {
            "model_name": "claude-haiku-4-5",
            "litellm_params": {
                "model": "anthropic/claude-haiku-4-5",
                "api_key": "sk-mock",
            },
        }
    ])
    ks._cached_app = None
    return "sk-test"


@pytest.fixture
def client(mock_litellm_key):
    """httpx.AsyncClient wrapping the kiko app for PRI4 test."""
    import httpx
    import litellm.proxy.kiko_server as ks
    ks._cached_app = None
    _client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ks.app),
        base_url="http://test",
    )
    yield _client
    ks._cached_app = None
