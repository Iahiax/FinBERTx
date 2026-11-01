from __future__ import annotations

import time
from urllib.parse import quote, urlencode

from finbertx.network import request_json
from finbertx.settings import Settings
from finbertx.strategy import currency_pair
from finbertx.types import Market


DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"
SUPPORTED_RESOLUTIONS = (
    "MINUTE",
    "MINUTE_5",
    "MINUTE_15",
    "MINUTE_30",
    "HOUR",
    "HOUR_4",
    "DAY",
    "WEEK",
)


class CapitalDemoClient:
    """Read-only Capital.com demo client for market discovery and history.

    This client intentionally has no order, position mutation, or account-setting
    method. Signals produced by the application remain hypothetical.
    """

    def __init__(self, settings: Settings):
        if not settings.capital_configured:
            raise ValueError("Capital.com demo credentials are incomplete.")
        if settings.capital_env != "demo":
            raise ValueError("Only Capital.com demo mode is supported.")
        self.settings = settings
        self._cst = ""
        self._security_token = ""
        self._session_started = 0.0

    def login(self) -> None:
        response = request_json(
            f"{DEMO_BASE_URL}/api/v1/session",
            method="POST",
            headers={"X-CAP-API-KEY": self.settings.capital_api_key},
            payload={
                "identifier": self.settings.capital_identifier,
                "password": self.settings.capital_password,
                "encryptedPassword": False,
            },
        )
        self._cst = response.headers.get("cst", "")
        self._security_token = response.headers.get("x-security-token", "")
        if not self._cst or not self._security_token:
            raise RuntimeError("Capital.com demo login did not return session tokens.")
        self._session_started = time.monotonic()

    def _headers(self) -> dict[str, str]:
        if not self._cst or time.monotonic() - self._session_started >= 8 * 60:
            self.login()
        return {"CST": self._cst, "X-SECURITY-TOKEN": self._security_token}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ):
        query = f"?{urlencode(params)}" if params else ""
        url = f"{DEMO_BASE_URL}/api/v1{path}{query}"
        return request_json(url, method=method, headers=self._headers()).data

    def all_markets(self) -> list[Market]:
        payload = self._request("GET", "/markets")
        rows = payload.get("markets", []) if isinstance(payload, dict) else []
        return [market for market in (Market.from_payload(row) for row in rows) if currency_pair(market)]

    def market_details(self, epic: str) -> dict:
        return self._request("GET", f"/markets/{quote(epic, safe='')}")

    def historical_prices(self, epic: str, resolution: str, maximum: int = 1000) -> list[dict]:
        if resolution not in SUPPORTED_RESOLUTIONS:
            raise ValueError(f"Unsupported Capital.com resolution: {resolution}")
        if not 1 <= maximum <= 1000:
            raise ValueError("Historical price limit must be between 1 and 1000.")
        payload = self._request(
            "GET",
            f"/prices/{quote(epic, safe='')}",
            params={"resolution": resolution, "max": str(maximum)},
        )
        rows = payload.get("prices", []) if isinstance(payload, dict) else []
        return rows if isinstance(rows, list) else []

    def positions(self) -> list[dict]:
        """Read currently open demo positions; never changes them."""
        payload = self._request("GET", "/positions")
        rows = payload.get("positions", []) if isinstance(payload, dict) else []
        return rows if isinstance(rows, list) else []
