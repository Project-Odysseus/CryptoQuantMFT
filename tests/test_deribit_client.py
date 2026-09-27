"""Deribit sign-in and read-only checks against a fake API; nothing reaches Deribit."""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest

import main
from src.execution.deribit_client import DeribitClient


class FakeDeribit:
    def __init__(self, scope: str = "account:read trade:read wallet:none mainaccount", *, busy_first: bool = False) -> None:
        self.scope, self.busy_first, self.calls = scope, busy_first, []

    def __call__(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        path, _, query = url.partition("?")
        method = path.split("/api/v2/", 1)[1]
        params = dict(urllib.parse.parse_qsl(query))
        self.calls.append((method, params, headers))
        if self.busy_first and len(self.calls) == 1:
            return {"jsonrpc": "2.0", "error": {"code": 10028, "message": "too_many_requests"}}
        if method == "public/auth":
            if params.get("client_secret") != "secret":
                return {"jsonrpc": "2.0", "error": {"code": 13004, "message": "invalid_credentials"}}
            return {"jsonrpc": "2.0", "result": {"access_token": "token-1", "expires_in": 900, "scope": self.scope, "token_type": "bearer"}}
        assert headers.get("Authorization") == "Bearer token-1"
        if method == "private/get_account_summary":
            if params["currency"] == "USDT":
                return {"jsonrpc": "2.0", "error": {"code": 11050, "message": "bad_request"}}
            return {"jsonrpc": "2.0", "result": {"currency": params["currency"], "equity": 0.0125, "available_funds": 0.0125, "margin_balance": 0.0125}}
        if method == "private/get_positions":
            return {"jsonrpc": "2.0", "result": [{"instrument_name": "BTC-27DEC26-100000-C", "size": 0.1}] if params["currency"] == "BTC" else []}
        raise AssertionError(method)


def _client(fake: FakeDeribit, secret: str = "secret", testnet: bool = False) -> DeribitClient:
    client = DeribitClient(client_id="id", client_secret=secret, testnet=testnet, transport=fake)
    client.sleep = lambda _: None
    return client


def test_sign_in_reads_the_scope_and_private_calls_carry_the_token() -> None:
    fake = FakeDeribit(busy_first=True)
    client = _client(fake, testnet=True)
    assert client.account_summary("BTC")["equity"] == pytest.approx(0.0125)
    assert client.permissions() == {"account": "read", "trade": "read", "wallet": "none"}
    assert client.base_url.startswith("https://test.deribit.com") and [call[0] for call in fake.calls].count("public/auth") == 2  # retried once when busy


def test_wrong_credentials_fail_plainly() -> None:
    with pytest.raises(RuntimeError, match="invalid_credentials"):
        _client(FakeDeribit(), secret="wrong").authenticate()
    with pytest.raises(ValueError):
        DeribitClient(client_id="", client_secret="")


def test_the_check_reports_balances_and_flags_a_key_that_can_move_funds(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(main.settings, "database_path", tmp_path / "db.sqlite")
    assert main.deribit_check(client=_client(FakeDeribit())) == 0
    printed = capsys.readouterr().out
    assert "signed in" in printed and "BTC: equity 0.01250000" in printed and "open positions 1" in printed and "USDT: no account summary" in printed

    assert main.deribit_check(client=_client(FakeDeribit(scope="account:read trade:read_write wallet:read_write"))) == 1
    printed = capsys.readouterr().out
    assert "PROBLEM" in printed and "wallet:read_write" in printed and "can trade" in printed


def test_the_check_explains_missing_keys(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main.settings, "deribit_client_id", "")
    assert main.deribit_check() == 2
    assert "DERIBIT_CLIENT_ID" in capsys.readouterr().out
