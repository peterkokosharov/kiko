# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet, Anthropic Opus
"""
KIKO auth — simple master-key check.
Replaces the full LiteLLM auth stack (DB, JWT, SSO, spend budgets).
Single secret from MASTER_KEY env var; no DB required.
"""
import hmac
import os
from fastapi import HTTPException, Request, WebSocket

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

def _master_key() -> str:
    # Read per request, not at import: run_server() copies the Credential Manager
    # key into os.environ only after this module has been imported.
    return os.environ.get("MASTER_KEY", "")


def _validate_token(token: str) -> UserAPIKeyAuth:
    master_key = _master_key()
    if not master_key:
        raise HTTPException(status_code=401, detail="No MASTER_KEY configured")
    if not token or not hmac.compare_digest(token.encode(), master_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing key")
    return UserAPIKeyAuth(api_key=token, user_role=LitellmUserRoles.PROXY_ADMIN)


async def user_api_key_auth(request: Request) -> UserAPIKeyAuth:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()
    else:
        token = (
            request.headers.get("x-api-key", "")
            or request.headers.get("x-goog-api-key", "")
        )
    return _validate_token(token)


async def user_api_key_auth_websocket(websocket: WebSocket) -> UserAPIKeyAuth:
    token = websocket.query_params.get("api_key", "")
    if not token:
        auth_header = websocket.headers.get("Authorization", "")
        token = auth_header[len("Bearer "):].strip() if auth_header.startswith("Bearer ") else ""
    return _validate_token(token)


async def _fetch_global_spend_with_event_coordination(*args, **kwargs) -> float:
    return 0.0
