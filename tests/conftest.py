"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_telegram_messages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stop tests from posting to the real Telegram chat configured in .env.

    Runtime tests build real orchestrators whose alerts go through
    TelegramNotifier; with credentials present that used to send actual
    messages. Sends are captured here instead (and report success).
    """
    from src.utils.telegram import TelegramNotifier

    sent: list[str] = []

    def capture(self: TelegramNotifier, message: str) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(TelegramNotifier, "_send_via_http", capture)
    return sent


@pytest.fixture(autouse=True)
def _isolated_runtime_storage(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep tests away from the real data/ directory.

    Code that builds the runtime uses settings.database_path and the perp
    sandbox state directory; without this, tests wrote trades into the real
    database and a saved sandbox position leaked from one run into the next.
    """
    import main
    from config import settings

    storage = tmp_path_factory.mktemp("runtime_storage")
    monkeypatch.setattr(settings, "database_path", storage / "test.db")
    monkeypatch.setattr(main, "PERP_SANDBOX_STATE_DIR", storage)
