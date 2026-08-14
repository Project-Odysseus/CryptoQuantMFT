"""Telegram notification transport for runtime alerts and trade updates."""

from __future__ import annotations

import json
import os
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

    def build_trade_update_message(
        self,
        *,
        strategy_name: str,
        trade_side: str,
        current_pnl: float,
        pnl_last_hour: float,
        position_side: str,
        max_drawdown_pct: float,
        distance_to_max_drawdown_point: float,
        diagnostics_url: str | None = None,
    ) -> str:
        """Create the structured message body for a trade-made notification."""
        diagnostics_text = diagnostics_url or "pending"
        return (
            "New trade executed\n"
            f"Strategy: {strategy_name}\n"
            f"Direction: {trade_side}\n"
            f"Current PnL: {current_pnl:+.2f}\n"
            f"PnL last hour: {pnl_last_hour:+.2f}\n"
            f"Long/Short: {position_side}\n"
            f"Current max drawdown: {max_drawdown_pct:.2%}\n"
            f"Distance to max drawdown point: {distance_to_max_drawdown_point:+.2f}\n"
            f"Diagnostics: {diagnostics_text}"
        )

    def send_trade_update(
        self,
        *,
        strategy_name: str,
        trade_side: str,
        current_pnl: float,
        pnl_last_hour: float,
        position_side: str,
        max_drawdown_pct: float,
        distance_to_max_drawdown_point: float,
        diagnostics_url: str | None = None,
    ) -> bool:
        """Send a trade-update summary message through the configured channel."""
        message = self.build_trade_update_message(
            strategy_name=strategy_name,
            trade_side=trade_side,
            current_pnl=current_pnl,
            pnl_last_hour=pnl_last_hour,
            position_side=position_side,
            max_drawdown_pct=max_drawdown_pct,
            distance_to_max_drawdown_point=distance_to_max_drawdown_point,
            diagnostics_url=diagnostics_url,
        )
        return self.send_message(message)

    def send_alert(self, *, event_type: str, message: str, metadata: dict[str, Any] | None = None) -> bool:
        """Send a structured alert payload to Telegram."""
        payload = {
            "event_type": event_type,
            "message": message,
            "metadata": metadata or {},
        }
        return self.send_message(str(payload))
