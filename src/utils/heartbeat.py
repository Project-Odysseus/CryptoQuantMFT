"""A dead-man's switch: ping an outside monitor while the runtime is healthy, so its silence raises the alarm.

A process can't report its own death. A crash, a closed laptop, a lost
network or a hung loop all look the same from inside: nothing happens. So the
runtime pings a URL at an outside service (healthchecks.io's free tier works,
and it can alert by Telegram, e-mail or SMS) after every good cycle. If the
pings stop for longer than the check's grace period, the service alerts.

URL conventions follow healthchecks.io: `<url>` for "alive", `<url>/start` when
the runtime starts, and `<url>/fail` with a short reason in the body when a
cycle fails or the runtime stops. Any service that accepts a GET/POST per ping
works for the plain "alive" ping.
"""

from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable

from src.utils.logger import logger

Transport = Callable[[str, bytes | None], None]


def _default_transport(url: str, body: bytes | None) -> None:
    request = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET", headers={"User-Agent": "QuantMultiStrat/0.1"})
    with urllib.request.urlopen(request, timeout=5):
        pass


class Heartbeat:
    """Pings `url` (at most every `min_interval` seconds for "alive"); a failed ping is logged, never raised."""

    def __init__(self, url: str, *, min_interval: float = 60.0, transport: Transport | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        """`transport(url, body)` replaces the HTTP call in tests."""
        self.url = url.rstrip("/")
        self.min_interval = min_interval
        self.transport = transport or _default_transport
        self.clock = clock
        self._last_alive: float | None = None

    def alive(self) -> bool:
        """The runtime completed a good cycle. Throttled; returns whether a ping was sent."""
        now = self.clock()
        if self._last_alive is not None and now - self._last_alive < self.min_interval:
            return False
        self._last_alive = now
        return self._send(self.url, None)

    def started(self) -> bool:
        """The runtime started (the monitor can measure how long a start takes)."""
        return self._send(f"{self.url}/start", None)

    def failed(self, reason: str) -> bool:
        """A cycle failed or the runtime stopped: alert now instead of waiting out the grace period."""
        self._last_alive = None
        return self._send(f"{self.url}/fail", reason[:1000].encode("utf-8"))

    def _send(self, url: str, body: bytes | None) -> bool:
        try:
            self.transport(url, body)
            return True
        except Exception as exc:  # noqa: BLE001 - monitoring must never break trading
            logger.warning("heartbeat_ping_failed url={} error={}", url.split("//")[-1].split("/")[0], exc)
            return False
