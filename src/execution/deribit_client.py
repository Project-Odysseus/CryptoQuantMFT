"""Authenticated Deribit API access (JSON-RPC over HTTPS), starting read-only: who the key is and what the account holds.

Deribit keys are a client id and a client secret. `public/auth` with the
client-credentials grant returns an access token, sent as a Bearer header on
private calls, and the key's **scope**: which permission groups it has, for
example ``account:read trade:read_write wallet:none``. The check reports the
scope first, because a key that can move funds (``wallet:read_write``) should
not live on a trading machine.

Only reads are used here (account summaries and positions). Order placement
comes later, behind the same live gates as Kraken, and first on the testnet
(``DERIBIT_TESTNET=true`` with testnet keys).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from src.utils.retry import TransientExchangeError, has_transient_marker, retry_call

LIVE_URL = "https://www.deribit.com/api/v2"
TEST_URL = "https://test.deribit.com/api/v2"
Transport = Callable[[str, dict[str, str]], dict[str, Any]]


def _default_transport(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "CryptoQuantMFT/0.1", "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:  # Deribit answers errors as JSON with a 400 status
        body = error.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise error from None


class DeribitClient:
    """Signed-in Deribit calls. Reads are retried on temporary failures; the token is renewed before it expires."""

    def __init__(self, *, client_id: str, client_secret: str, testnet: bool = False, transport: Transport | None = None) -> None:
        """`transport(url, headers)` replaces the HTTP call in tests."""
        if not client_id or not client_secret:
            raise ValueError("DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET are required")
        self.client_id = client_id
        self._client_secret = client_secret
        self.base_url = TEST_URL if testnet else LIVE_URL
        self.testnet = testnet
        self.transport = transport or _default_transport
        self.sleep: Callable[[float], None] = time.sleep
        self._token: str | None = None
        self._expires_at = 0.0
        self.scope: str = ""

    def _get(self, method: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        url = f"{self.base_url}/{method}?{urllib.parse.urlencode(params)}"
        payload = self.transport(url, headers or {})
        if "error" in payload:
            error = payload["error"]
            message = f"Deribit {method} failed: {error.get('message', error)} (code {error.get('code')})" if isinstance(error, dict) else f"Deribit {method} failed: {error}"
            if has_transient_marker(message) or (isinstance(error, dict) and error.get("code") in {10028, 10040, 10041, 10047, 11051}):  # rate limits, retry later, maintenance
                raise TransientExchangeError(message, payload)
            raise RuntimeError(message)
        return payload.get("result")

    def authenticate(self) -> dict[str, Any]:
        """Sign in with the client-credentials grant; returns the token details including the key's scope."""
        result = retry_call(lambda: self._get("public/auth", {"grant_type": "client_credentials", "client_id": self.client_id,
                                                              "client_secret": self._client_secret}), label="deribit auth", sleep=self.sleep)
        self._token = result["access_token"]
        self._expires_at = time.monotonic() + float(result.get("expires_in", 0)) * 0.9
        self.scope = str(result.get("scope", ""))
        return result

    def private(self, method: str, params: dict[str, Any] | None = None, *, retry: bool = True) -> Any:
        """A private call (e.g. ``private/get_account_summary``), signing in first if needed.

        Reads and cancels are retried on temporary failures. Order placement passes `retry=False`: a timed-out
        order may already be live, so it is looked up by its label instead of being sent again.
        """
        if self._token is None or time.monotonic() >= self._expires_at:
            self.authenticate()
        call = lambda: self._get(method, params or {}, {"Authorization": f"Bearer {self._token}"})  # noqa: E731
        return retry_call(call, label=f"deribit {method}", sleep=self.sleep) if retry else call()

    def permissions(self) -> dict[str, str]:
        """The key's permission groups from its scope, e.g. {"account": "read", "trade": "read_write", "wallet": "none"}."""
        groups = {}
        for item in self.scope.split():
            name, _, level = item.partition(":")
            if name in {"account", "trade", "wallet", "block_trade", "custody"}:
                groups[name] = level
        return groups

    def account_summary(self, currency: str) -> dict[str, Any]:
        """Equity, balance, available funds and margin for one currency's sub-account (BTC, ETH, USDC, USDT...)."""
        return self.private("private/get_account_summary", {"currency": currency, "extended": "true"})

    def positions(self, currency: str) -> list[dict[str, Any]]:
        """Open positions (options and futures) settled in `currency`."""
        return list(self.private("private/get_positions", {"currency": currency}) or [])
