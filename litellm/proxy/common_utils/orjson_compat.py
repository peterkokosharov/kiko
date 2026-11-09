# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""orjson is an optional extra (`perf`); fall back to stdlib json with the same call shape.

Usage: ``from litellm.proxy.common_utils import orjson_compat as orjson``.
"""

try:
    from orjson import OPT_NON_STR_KEYS, JSONDecodeError, dumps, loads  # noqa: F401
except ImportError:
    import json
    from typing import Any, Callable, Optional

    JSONDecodeError = json.JSONDecodeError  # type: ignore[misc,assignment]
    OPT_NON_STR_KEYS = 16  # accepted and ignored: json already coerces scalar keys

    def loads(data: "str | bytes | bytearray | memoryview") -> Any:  # type: ignore[misc]
        if isinstance(data, memoryview):
            data = data.tobytes()
        return json.loads(data)

    def dumps(  # type: ignore[misc]
        obj: Any, default: Optional[Callable[[Any], Any]] = None, option: Optional[int] = None
    ) -> bytes:
        return json.dumps(obj, default=default, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


try:
    import orjson as _orjson_probe  # noqa: F401
    from fastapi.responses import ORJSONResponse  # noqa: F401  # pyright: ignore[reportDeprecated]
except ImportError:
    import json as _json
    from typing import Any as _Any

    from fastapi.responses import JSONResponse as _JSONResponse

    class ORJSONResponse(_JSONResponse):  # type: ignore[no-redef]
        """Stdlib-json stand-in for fastapi's ORJSONResponse (which asserts orjson is installed)."""

        def render(self, content: _Any) -> bytes:
            return _json.dumps(
                content, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")
            ).encode("utf-8")
