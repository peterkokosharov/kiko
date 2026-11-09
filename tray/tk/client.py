# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""HTTP+JSON client for kiko-lite (blocking; call it from a worker thread, never from the Tk loop)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

TIMEOUT = 5.0


class Unreachable(Exception):
    """kiko-lite did not answer (refused, timeout, DNS, bad JSON)."""


class AuthNeeded(Exception):
    """HTTP 401/403: a client key (--key / KIKO_TK_KEY) is needed or wrong."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the x-api-key header must not be sent anywhere else."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # urllib then raises the 3xx as HTTPError


_OPENER = urllib.request.build_opener(_NoRedirect)


def get_json(base: str, path: str, key: str = "", timeout: float = TIMEOUT) -> Any:
    req = urllib.request.Request(base.rstrip("/") + path, headers={"Accept": "application/json"})
    if key:
        req.add_header("x-api-key", key)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            raise Unreachable(f"redirect refused (HTTP {e.code})") from e
        if e.code in (401, 403):
            raise AuthNeeded(f"HTTP {e.code}") from e
        raise Unreachable(f"HTTP {e.code}") from e
    except (OSError, ValueError) as e:  # URLError, timeout, ConnectionError, JSONDecodeError
        raise Unreachable(str(getattr(e, "reason", e))) from e


def health(base: str, timeout: float = TIMEOUT) -> bool:
    try:
        return get_json(base, "/health", "", timeout).get("status") == "ok"
    except (Unreachable, AuthNeeded, AttributeError):
        return False


def fetch_status(base: str, key: str = "", timeout: float = TIMEOUT) -> dict[str, Any]:
    """GET /ui/api/status: {"server": {...}, "models": [...], "requests": [...]}."""
    data = get_json(base, "/ui/api/status", key, timeout)
    if not isinstance(data, dict) or "models" not in data:
        raise Unreachable("unexpected answer from /ui/api/status")
    return data
