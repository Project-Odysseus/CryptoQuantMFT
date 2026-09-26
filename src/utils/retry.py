"""Retries with exponential backoff for exchange calls that failed for a temporary reason.

A timeout, a dropped connection, HTTP 429/5xx or an exchange saying it is busy
usually clears within seconds. Without a retry, one such blip costs a whole
cycle (a missed fill check, a skipped rebalance, a false stale-data alert).

**Only calls that are safe to repeat are retried:** reads, and cancels
(cancelling twice is harmless). Placing an order is never retried here: a
timed-out order may already be live on the exchange, and sending it again could
double the position. Those paths look the order up by its client id instead.
"""

from __future__ import annotations

import http.client
import json
import random
import time
import urllib.error
from collections.abc import Callable
from typing import Any, TypeVar

from src.utils.logger import logger

T = TypeVar("T")

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.5  # seconds; doubles on each retry (0.5, 1.0), plus up to 10% jitter
RETRY_MAX_DELAY = 4.0
TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
# Exchange error texts that mean "try again shortly" (Kraken spot codes, Kraken Futures error names)
TRANSIENT_MARKERS = (
    "EService:Unavailable", "EService:Busy", "EService:Deadline elapsed", "EAPI:Rate limit exceeded", "EAPI:Invalid nonce",
    "apiLimitExceeded", "nonceBelowThreshold", "nonceDuplicate", "Server Error", "timed out", "temporarily unavailable",
)


class TransientExchangeError(RuntimeError):
    """The exchange answered with an error that should clear on its own; `payload` is its answer."""

    def __init__(self, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.payload = payload


def has_transient_marker(text: Any) -> bool:
    """Whether an exchange error message says the failure is temporary."""
    if isinstance(text, (list, tuple)):
        return any(has_transient_marker(item) for item in text)
    return any(marker.lower() in str(text).lower() for marker in TRANSIENT_MARKERS)


def is_transient(exc: BaseException) -> bool:
    """Whether `exc` (or the error it wraps) is a temporary network or exchange failure worth retrying."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TransientExchangeError):
            return True
        if isinstance(current, urllib.error.HTTPError):
            return current.code in TRANSIENT_HTTP_CODES
        if isinstance(current, (TimeoutError, ConnectionError, urllib.error.URLError, http.client.HTTPException, json.JSONDecodeError)):
            return True
        current = current.__cause__ or current.__context__
    return has_transient_marker(str(exc))


def retry_call(
    fn: Callable[[], T],
    *,
    label: str,
    attempts: int = RETRY_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    sleep: Callable[[float], None] | None = None,
    jitter: float = 0.1,
) -> T:
    """Call `fn`, retrying temporary failures with exponential backoff; any other error, or the last one, is raised."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified below
            if attempt >= attempts or not is_transient(exc):
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay += random.uniform(0.0, delay * jitter) if jitter else 0.0
            logger.warning("http_retry label={} attempt={}/{} wait={:.2f}s error={}", label, attempt, attempts, delay, str(exc)[:200])
            (sleep or time.sleep)(delay)
    raise AssertionError("unreachable")
