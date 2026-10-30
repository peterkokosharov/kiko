# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
"""Retry policy for slow, flaky free providers (exponential backoff, full jitter, Retry-After).

``send_with_retry`` retries only *before* a body byte was consumed: it never reads a
successful response, so the caller can still stream it. Long waits are split into
slices of at most ``PING_INTERVAL`` seconds and ``on_wait`` is called before each
slice (the server uses it to send SSE pings so Claude Code does not give up).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

from .http_client import ConnectError, ConnectionLost, HttpTimeout, Response

PING_INTERVAL = 10.0


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 20.0
    # budget is a SOFT limit (seconds): no wait runs past it, but a last attempt may start just
    # before it ends, and an attempt itself can take up to its request timeout.
    budget: float = 90.0
    retry_statuses: frozenset[int] = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
    jitter: bool = True  # full jitter: uniform(0, backoff)
    rng: Callable[[], float] = field(default=random.random, repr=False, compare=False)

    def delay_for(self, attempt: int, retry_after_header: str | None = None, *,
                  remaining: float | None = None, now: float | None = None) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based)."""
        delay = None
        if retry_after_header:
            delay = _parse_retry_after(retry_after_header.strip(), time.time() if now is None else now)
        if delay is None:  # no (valid) Retry-After: exponential backoff, capped by max_delay
            cap = min(self.max_delay, self.base_delay * (2 ** max(attempt - 1, 0)))
            delay = cap * self.rng() if self.jitter else cap
            delay = min(delay, self.max_delay)
        delay = max(delay, 0.0)
        if remaining is not None:
            delay = min(delay, max(remaining, 0.0))
        return delay


def _parse_retry_after(value: str, now: float) -> float | None:
    try:
        v = float(value)
        return v if v == v else None  # NaN is not a delay
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value).timestamp() - now
    except (TypeError, ValueError, IndexError):
        return None


def parse_retry_after(value: str, now: float | None = None) -> float | None:
    """Seconds from a Retry-After value (delta seconds or HTTP date); None when it is neither."""
    return _parse_retry_after(value.strip(), time.time() if now is None else now)


def send_with_retry(
    send: Callable[[], Response],
    policy: RetryPolicy | None = None,
    *,
    on_wait: Callable[[float, int, str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[Response, int]:
    """Call ``send`` until it gives a non-retryable response; return (response, attempts used).

    Retries on ConnectError, HttpTimeout, ConnectionLost and ``policy.retry_statuses``. When attempts or the
    time budget run out the last response is returned (or the last exception re-raised).
    """
    policy = policy or RetryPolicy()
    start = clock()
    attempt = 0
    while True:
        attempt += 1
        resp: Response | None = None
        retry_after = None
        try:
            resp = send()
        except (ConnectError, HttpTimeout, ConnectionLost) as exc:
            reason = type(exc).__name__
            err: Exception | None = exc
        else:
            if resp.status not in policy.retry_statuses:
                return resp, attempt
            reason, err = f"http {resp.status}", None
            retry_after = resp.headers.get("Retry-After")
        remaining = policy.budget - (clock() - start)
        if attempt >= policy.attempts or remaining <= 0:
            if resp is not None:
                return resp, attempt
            assert err is not None
            raise err
        if retry_after and resp is not None:
            wanted = _parse_retry_after(retry_after.strip(), time.time())
            if wanted is not None and wanted > remaining:
                return resp, attempt  # the server asks for more than our budget allows: stop now
        delay = policy.delay_for(attempt, retry_after, remaining=remaining)
        if resp is not None:
            resp.close()
        left = delay
        while left > 0:
            piece = min(left, PING_INTERVAL)
            if on_wait is not None:
                on_wait(piece, attempt, reason)
            sleep(piece)
            left -= piece
