"""Tests for the Phase 1 infrastructure additions."""

from __future__ import annotations

import sys

from src.utils.telemetry import WebSocketReconnectHandler, install_exception_hooks


def test_websocket_reconnect_handler_backoff() -> None:
    """The reconnect helper should grow delay exponentially up to the cap."""
    handler = WebSocketReconnectHandler(initial_delay_seconds=0.5, max_delay_seconds=2.0, backoff_factor=2.0, jitter=0.0)

    assert handler.handle_disconnect() == 0.5
    assert handler.handle_disconnect() == 1.0
    assert handler.handle_disconnect() == 2.0

    handler.reset()
    assert handler.handle_disconnect() == 0.5


def test_exception_hooks_install_without_error() -> None:
    """Installing the hooks should not fail and should replace the default handler."""
    install_exception_hooks()
    assert sys.excepthook is not sys.__excepthook__
