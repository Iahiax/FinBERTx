from __future__ import annotations

import time
from urllib.parse import quote, urlencode

from finbertx.network import ApiError, request_json
from finbertx.settings import Settings
from finbertx.strategy import currency_pair
from finbertx.types import Market, TradeSignal


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
    """Capital.com API client fixed to the demo host; no live URL can be configured."""

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
            raise ApiError("Capital.com demo login did not return session tokens.")
        self._session_started = time.monotonic()

    def _headers(self) -> dict[str, str]:
        if not self._cst or time.monotonic() - self._session_started >= 8 * 60:
            self.login()
        return {
            "CST": self._cst,
            "X-SECURITY-TOKEN": self._security_token,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict | None = None,
    ):
        query = f"?{urlencode(params)}" if params else ""
        url = f"{DEMO_BASE_URL}/api/v1{path}{query}"
        return request_json(
            url,
            method=method,
            headers=self._headers(),
            payload=payload,
        ).data

    def all_markets(self) -> list[Market]:
        payload = self._request("GET", "/markets")
        rows = payload.get("markets", []) if isinstance(payload, dict) else []
        markets = [Market.from_payload(row) for row in rows]
        return [market for market in markets if currency_pair(market)]

    def market_details(self, epic: str) -> dict:
        return self._request("GET", f"/markets/{quote(epic, safe='')}")

    def historical_prices(
        self, epic: str, resolution: str, maximum: int = 1000
    ) -> list[dict]:
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
        payload = self._request("GET", "/positions")
        rows = payload.get("positions", []) if isinstance(payload, dict) else []
        return rows if isinstance(rows, list) else []

    def open_demo_position(
        self,
        signal: TradeSignal,
        market: Market,
    ) -> dict:
        if not self.settings.enable_demo_orders:
            raise RuntimeError("Demo order execution is disabled.")
        if market.market_status != "TRADEABLE":
            raise RuntimeError("Market is not currently tradeable.")

        size = self.settings.capital_demo_order_size
        if not 0 < size <= 1:
            raise RuntimeError("Demo order size must be above 0 and at most 1.")
        if market.min_deal_size and size < market.min_deal_size:
            raise RuntimeError(
                "Configured demo size is below the broker's market minimum."
            )
        if market.max_deal_size and size > market.max_deal_size:
            raise RuntimeError(
                "Configured demo size exceeds the broker's market maximum."
            )
        if size > 1.0:
            raise RuntimeError("The project hard cap is one demo contract.")

        entry = market.offer if signal.direction == "BUY" else market.bid
        if entry is None or entry <= 0:
            raise RuntimeError("A valid Capital.com bid/offer quote is unavailable.")

        # Capital.com reports this rule as a percentage, not a price distance.
        stop_percent = max(
            self.settings.stop_distance_percent,
            market.min_stop_percent,
        )
        if stop_percent > 2 or stop_percent > market.max_stop_percent:
            raise RuntimeError("Market stop-distance rules exceed the project cap.")
        target_percent = stop_percent * self.settings.take_profit_multiple
        if target_percent > 2 or target_percent > market.max_stop_percent:
            raise RuntimeError("Market take-profit distance exceeds the project cap.")
        places = max(0, min(10, market.decimal_places))
        fraction = stop_percent / 100
        target_fraction = target_percent / 100

        if signal.direction == "BUY":
            stop_level = entry * (1 - fraction)
            profit_level = entry * (1 + target_fraction)
        elif signal.direction == "SELL":
            stop_level = entry * (1 + fraction)
            profit_level = entry * (1 - target_fraction)
        else:
            raise ValueError("Signal direction must be BUY or SELL.")

        order = self._request(
            "POST",
            "/positions",
            payload={
                "epic": signal.epic,
                "direction": signal.direction,
                "size": size,
                "guaranteedStop": False,
                "stopLevel": round(stop_level, places),
                "profitLevel": round(profit_level, places),
            },
        )
        deal_reference = str(order.get("dealReference") or "")
        if not deal_reference:
            raise ApiError("Capital.com did not return a demo deal reference.")

        # A created position first returns an order reference; confirmation is mandatory.
        for attempt in range(6):
            confirmation = self._request(
                "GET", f"/confirms/{quote(deal_reference, safe='')}"
            )
            status = str(confirmation.get("dealStatus") or "").upper()
            if status in {"ACCEPTED", "REJECTED"}:
                if status != "ACCEPTED":
                    raise ApiError("Capital.com rejected the demo position.")
                return confirmation
            if attempt < 5:
                time.sleep(0.5)
        raise ApiError(
            "Capital.com demo order was not confirmed; inspect the demo account "
            "before retrying."
        )
