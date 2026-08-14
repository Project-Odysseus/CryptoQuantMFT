"""Simple kill-switch controller for stopping live-style trading safely."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.storage.trade_logger import TradeLogger


class KillSwitchController:
    """Persist and activate a runtime kill switch for order cancellation and neutralization."""

    def __init__(self, *, state_file: str | Path | None = None, trade_logger: TradeLogger | None = None) -> None:
        self.state_file = Path(state_file or "data/kill_switch_state.json")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.trade_logger = trade_logger
        self._state = self._load_state()

    def activate(self, reason: str, *, execution_adapter: Any | None = None, trade_logger: TradeLogger | None = None) -> dict[str, Any]:
        self._state = {
            "active": True,
            "reason": reason,
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "orders_cancelled": [],
            "account_snapshot": None,
            "neutralized": False,
        }

        if execution_adapter is not None:
            self._state["orders_cancelled"] = self._cancel_open_orders(execution_adapter)
            self._state["account_snapshot"] = getattr(execution_adapter, "get_account_snapshot", lambda: None)()
            self._state["neutralized"] = True

        self._persist_state()

        logger = trade_logger or self.trade_logger
        if logger is not None:
            logger.log_event(
                timestamp=datetime.now(timezone.utc),
                level="WARNING",
                event_type="kill_switch_activated",
                message=reason,
                source="risk",
                metadata={"active": True, "orders_cancelled": len(self._state["orders_cancelled"])},
            )
        return self.get_state()

    def is_active(self) -> bool:
        return bool(self._state.get("active"))

    def get_state(self) -> dict[str, Any]:
        return dict(self._state)

    def reset(self) -> None:
        self._state = {"active": False, "reason": None, "triggered_at": None, "orders_cancelled": [], "account_snapshot": None, "neutralized": False}
        self._persist_state()

    def _cancel_open_orders(self, execution_adapter: Any) -> list[dict[str, Any]]:
        cancelled: list[dict[str, Any]] = []
        orders = getattr(execution_adapter, "list_orders", lambda: [])()
        for order in orders:
            order_id = getattr(order, "order_id", None)
            if not order_id:
                continue
            status = getattr(order, "status", None)
            if status in {"FILLED", "CANCELED", "REJECTED", "NOT_FOUND"}:
                continue
            report = execution_adapter.cancel_order(order_id=order_id)
            cancelled.append({"order_id": order_id, "status": getattr(report, "status", None)})
        return cancelled

    def _load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"active": False, "reason": None, "triggered_at": None, "orders_cancelled": [], "account_snapshot": None, "neutralized": False}

        try:
            with self.state_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
                if isinstance(payload, dict):
                    return {
                        "active": bool(payload.get("active", False)),
                        "reason": payload.get("reason"),
                        "triggered_at": payload.get("triggered_at"),
                        "orders_cancelled": payload.get("orders_cancelled", []),
                        "account_snapshot": payload.get("account_snapshot"),
                        "neutralized": bool(payload.get("neutralized", False)),
                    }
        except (OSError, json.JSONDecodeError):
            return {"active": False, "reason": None, "triggered_at": None, "orders_cancelled": [], "account_snapshot": None, "neutralized": False}

        return {"active": False, "reason": None, "triggered_at": None, "orders_cancelled": [], "account_snapshot": None, "neutralized": False}

    def _persist_state(self) -> None:
        with self.state_file.open("w", encoding="utf-8") as handle:
            json.dump(self._state, handle, indent=2, default=str)
