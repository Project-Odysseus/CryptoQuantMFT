"""Telegram notification transport for runtime alerts and trade updates."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib import request

from config import settings
from src.utils.logger import logger


class TelegramNotifier:
    """Send Telegram alerts when bot credentials are available."""

    def __init__(self, *, bot_token: str | None = None, chat_id: str | None = None) -> None:
        """Initialize the notifier with optional explicit values or environment defaults."""
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or settings.telegram_bot_token
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID") or settings.telegram_chat_id

    def is_configured(self) -> bool:
        """Return whether the notifier has the minimum required configuration."""
        return bool(self.bot_token and self.chat_id)

    def send_message(self, message: str) -> bool:
        """Send a message when configured, otherwise log a skip notice."""
        if not self.is_configured():
            logger.info("telegram_notifier_skipped message={}", message)
            return False

        try:
            success = self._send_via_http(message)
        except Exception as exc:  # pragma: no cover - exercised through runtime smoke tests
            logger.warning("telegram_notifier_failed error={}", exc)
            return False

        if success:
            logger.info("telegram_notifier_sent message={}", message)
            return True

        logger.warning("telegram_notifier_failed message={}", message)
        return False

    def _send_via_http(self, message: str) -> bool:
        """Deliver the message through the Telegram Bot API."""
        payload = json.dumps({"chat_id": self.chat_id, "text": message, "disable_web_page_preview": True}).encode("utf-8")
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        req = request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8")
            payload_response = json.loads(body) if body else {}
            return bool(payload_response.get("ok", False))

    def send_trade_alert(self, alert: "TradeAlert") -> bool:
        """Send one trade alert (see `format_trade_alert`)."""
        return self.send_message(format_trade_alert(alert))

    def send_alert(self, *, event_type: str, message: str, metadata: dict[str, Any] | None = None) -> bool:
        """Send a runtime alert as readable text: the event, the message, then one line per metadata item."""
        lines = [f"ALERT {event_type}", message]
        lines += [f"{key}: {value}" for key, value in (metadata or {}).items()]
        return self.send_message("\n".join(lines))


MODE_LABELS = {"paper": "PAPER", "live_dry_run": "DRY RUN", "live": "LIVE"}
# Why an order was placed, in words. Entries and exits come from the strategy's signal; the rest are risk stops
# that close a position whatever the signal says (src/risk/controls.py).
TRADE_REASONS = {
    "enter_long": "entry: the signal turned long",
    "enter_short": "entry: the signal turned short",
    "exit_long": "exit: the signal left long",
    "exit_short": "exit: the signal left short",
    "time_stop": "risk stop: held for the maximum number of bars",
    "atr_stop_loss": "risk stop: price moved the ATR stop distance against the position",
    "position_drawdown_stop": "risk stop: the position lost its maximum allowed share",
    "liquidation_buffer": "risk stop: price came too close to the liquidation price",
    "hard_stop": "risk stop: account drawdown hard stop",
    "hard_stop_drawdown": "risk stop: account drawdown hard stop",
    "drawdown_limit": "risk stop: account drawdown limit",
    "daily_loss_limit": "risk stop: daily loss limit",
    "circuit_breaker": "risk stop: circuit breaker",
}


@dataclass(frozen=True, slots=True)
class TradeAlert:
    """Everything a trade message says: what traded, why, and where the account stands after it.

    Attributes:
        intent: Why the order exists (a `TRADE_REASONS` key, or any risk reason code).
        signal: The strategy's signal on the bar that caused it (1 long, -1 short, 0 flat).
        position_size: Signed position after the trade (negative = short).
    """

    mode: str
    strategy_name: str
    side: str
    size: float
    price: float
    symbol: str | None = None
    strategy_params: dict[str, Any] = field(default_factory=dict)
    intent: str | None = None
    signal: float | None = None
    fee: float = 0.0
    position_size: float = 0.0
    avg_entry_price: float | None = None
    equity: float | None = None
    pnl: float | None = None
    pnl_last_hour: float | None = None
    drawdown_pct: float | None = None
    timestamp: datetime | None = None


def format_trade_alert(alert: TradeAlert) -> str:
    """The trade message: a headline, then why, the strategy, the position and the account, one line each."""
    mode = MODE_LABELS.get(alert.mode, alert.mode.upper())
    symbol = alert.symbol or "?"
    lines = [f"[{mode}] {alert.side.upper()} {alert.size:.8g} {symbol} @ {alert.price:,.2f}"]
    why = TRADE_REASONS.get(alert.intent or "", alert.intent or "not recorded")
    signal = "n/a" if alert.signal is None else "0" if alert.signal == 0 else f"{alert.signal:+g}"
    lines.append(f"Why: {why} ({alert.strategy_name} signal {signal})")
    params = ", ".join(f"{key}={value}" for key, value in alert.strategy_params.items())
    lines.append(f"Strategy: {alert.strategy_name}" + (f" ({params})" if params else ""))
    if alert.position_size:
        side = "long" if alert.position_size > 0 else "short"
        entry = f", avg entry {alert.avg_entry_price:,.2f}" if alert.avg_entry_price else ""
        lines.append(f"Position now: {side} {abs(alert.position_size):.8g} {symbol}{entry}")
    else:
        lines.append("Position now: flat")
    account = []
    if alert.equity is not None:
        account.append(f"equity {alert.equity:,.2f}")
    if alert.pnl is not None:
        account.append(f"P&L {alert.pnl:+,.2f}")
    if alert.pnl_last_hour is not None:
        account.append(f"last hour {alert.pnl_last_hour:+,.2f}")
    if alert.drawdown_pct is not None:
        account.append(f"drawdown {alert.drawdown_pct:.1%}")
    if account:
        lines.append("Account: " + ", ".join(account))
    when = f" at {alert.timestamp:%Y-%m-%d %H:%M} UTC" if alert.timestamp else ""
    lines.append(f"Fee {alert.fee:,.2f}{when}")
    return "\n".join(lines)
