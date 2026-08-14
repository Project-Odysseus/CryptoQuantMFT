"""Runtime telemetry helpers for startup resilience and error visibility."""

from __future__ import annotations

import random
import sys
import threading
from typing import Any

from loguru import logger


def install_exception_hooks() -> None:
    """Register global hooks that log uncaught exceptions and thread failures."""

    def _log_exception(exc_type: type[BaseException], exc_value: BaseException | None, exc_traceback: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.opt(exception=(exc_type, exc_value, exc_traceback)).critical("Unhandled exception")

    def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
        logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).critical("Unhandled exception in thread")

    sys.excepthook = _log_exception
    threading.excepthook = _log_thread_exception


class WebSocketReconnectHandler:
    """Exposes a simple backoff strategy for transient WebSocket disconnects."""

    def __init__(
        self,
        initial_delay_seconds: float = 1.0,
        max_delay_seconds: float = 60.0,
        backoff_factor: float = 2.0,
        jitter: float = 0.2,
    ) -> None:
        """Initialize the object with its runtime state."""
        self.initial_delay_seconds = initial_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.backoff_factor = backoff_factor
        self.jitter = jitter
        self.attempts = 0
        self.last_delay_seconds = 0.0

    def handle_disconnect(self) -> float:
        """Return the next reconnect delay and advance the retry counter."""
        self.attempts += 1
        raw_delay = min(
            self.initial_delay_seconds * (self.backoff_factor ** (self.attempts - 1)),
            self.max_delay_seconds,
        )
        jitter_amount = raw_delay * self.jitter
        self.last_delay_seconds = raw_delay + random.uniform(0.0, jitter_amount)
        return self.last_delay_seconds

    def reset(self) -> None:
        """Reset the reconnect counters after a successful connection."""
        self.attempts = 0
        self.last_delay_seconds = 0.0
