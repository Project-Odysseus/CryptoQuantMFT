"""Tests for the Telegram notifier."""

from __future__ import annotations

import src.utils.telegram as telegram_module
from src.utils.telegram import TelegramNotifier


def test_telegram_notifier_skips_when_unconfigured(monkeypatch) -> None:
    """Unconfigured notifier should skip sending."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(telegram_module.settings, "telegram_bot_token", "", raising=False)
    monkeypatch.setattr(telegram_module.settings, "telegram_chat_id", "", raising=False)

    notifier = TelegramNotifier(bot_token=None, chat_id=None)

    assert notifier.is_configured() is False
    assert notifier.send_message("hello") is False


def test_telegram_notifier_reports_ready_when_configured(monkeypatch) -> None:
    """Configured notifier should attempt delivery and report success when configured."""
    notifier = TelegramNotifier(bot_token="token", chat_id="chat")
    monkeypatch.setattr(notifier, "_send_via_http", lambda _message: True)

    assert notifier.is_configured() is True
    assert notifier.send_message("hello") is True


def test_telegram_notifier_builds_trade_update_message() -> None:
    """Trade updates should include the structured PnL and drawdown summary."""
    notifier = TelegramNotifier(bot_token="token", chat_id="chat")

    message = notifier.build_trade_update_message(
        strategy_name="momentum_breakout",
        trade_side="buy",
        current_pnl=123.45,
        pnl_last_hour=12.3,
        position_side="long",
        max_drawdown_pct=0.12,
        distance_to_max_drawdown_point=45.6,
        diagnostics_url="https://example.test/diagnostics",
    )

    assert "New trade executed" in message
    assert "Strategy: momentum_breakout" in message
    assert "Direction: buy" in message
    assert "Current PnL: +123.45" in message
    assert "Long/Short: long" in message
    assert "Diagnostics: https://example.test/diagnostics" in message
