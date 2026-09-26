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


def test_a_trade_message_says_what_traded_why_and_where_the_account_stands() -> None:
    from datetime import datetime, timezone

    from src.utils.telegram import TradeAlert, format_trade_alert

    entry = format_trade_alert(TradeAlert(
        mode="paper", strategy_name="moving_average_crossover", strategy_params={"short_window": 4, "long_window": 48},
        side="buy", size=0.0123, price=50123.4, symbol="BTC/USD", intent="enter_long", signal=1.0, fee=0.62,
        position_size=0.0123, avg_entry_price=50123.4, equity=10050.12, pnl=50.12, pnl_last_hour=3.2, drawdown_pct=0.012,
        timestamp=datetime(2026, 9, 26, 16, tzinfo=timezone.utc),
    ))
    assert entry.splitlines() == [
        "[PAPER] BUY 0.0123 BTC/USD @ 50,123.40",
        "Why: entry: the signal turned long (moving_average_crossover signal +1)",
        "Strategy: moving_average_crossover (short_window=4, long_window=48)",
        "Position now: long 0.0123 BTC/USD, avg entry 50,123.40",
        "Account: equity 10,050.12, P&L +50.12, last hour +3.20, drawdown 1.2%",
        "Fee 0.62 at 2026-09-26 16:00 UTC",
    ]
    stop = format_trade_alert(TradeAlert(mode="live", strategy_name="keltner_breakout", side="sell", size=0.05, price=48000.0, symbol="BTC/USD", intent="atr_stop_loss", signal=1.0))
    assert stop.startswith("[LIVE] SELL 0.05 BTC/USD") and "risk stop: price moved the ATR stop distance" in stop and "Position now: flat" in stop
    unknown = format_trade_alert(TradeAlert(mode="live_dry_run", strategy_name="x", side="buy", size=1.0, price=1.0, intent="new_rule"))
    assert unknown.startswith("[DRY RUN]") and "Why: new_rule (x signal n/a)" in unknown


def test_alerts_are_sent_as_readable_lines(monkeypatch) -> None:
    notifier = TelegramNotifier(bot_token="token", chat_id="chat")
    sent: list[str] = []
    monkeypatch.setattr(notifier, "_send_via_http", lambda message: sent.append(message) or True)
    notifier.send_alert(event_type="stale_data", message="No fresh bar for 3 intervals", metadata={"symbol": "BTC/USD", "age_seconds": 900})
    assert sent == ["ALERT stale_data\nNo fresh bar for 3 intervals\nsymbol: BTC/USD\nage_seconds: 900"]


def test_the_telegram_test_flag_sends_one_sample_trade(monkeypatch, capsys) -> None:
    import main

    sent: list[str] = []
    monkeypatch.setattr(TelegramNotifier, "is_configured", lambda self: True)
    monkeypatch.setattr(TelegramNotifier, "_send_via_http", lambda self, message: sent.append(message) or True)
    assert main.telegram_test() == 0
    assert len(sent) == 1 and sent[0].startswith("[TEST] BUY 0.001 BTC/USD") and "Why: entry: the signal turned long" in sent[0]
    assert "Sent a TEST trade message" in capsys.readouterr().out

    monkeypatch.setattr(TelegramNotifier, "is_configured", lambda self: False)
    assert main.telegram_test() == 1
