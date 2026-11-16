from __future__ import annotations

import time
import threading
from collections.abc import Callable
from urllib.parse import quote, urlencode

from finbertx.network import request_json
from finbertx.settings import LIVE_ACK_TEXT, Settings
from finbertx.types import Market

DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"
LIVE_BASE_URL = "https://api-capital.backend-capital.com"
DEFAULT_STREAM_URL = "wss://api-streaming-capital.backend-capital.com/connect"
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


class CapitalClient:
    """Capital.com client with read-only-by-default market access.

    Order placement is available only behind the explicit Demo/Live switches;
    the client never changes mode by itself and validates its configured host.
    """

    def __init__(self, settings: Settings, environment: str = "demo"):
        if environment not in {"demo", "live"}:
            raise ValueError("Capital.com environment must be demo or live.")
        credentials = settings.capital_credentials_for(environment)
        if not all(credentials):
            raise ValueError(f"Capital.com {environment} credentials are incomplete.")
        if environment == "live" and (
            not settings.live_trading_enabled
            or settings.live_trading_ack != LIVE_ACK_TEXT
        ):
            raise ValueError("Live trading is locked by configuration.")
        self.settings = settings
        self.environment = environment
        self.base_url = DEMO_BASE_URL if environment == "demo" else LIVE_BASE_URL
        self.api_key, self.identifier, self.password = credentials
        self._cst = ""
        self._security_token = ""
        self._session_started = 0.0
        self._streaming_host = ""
        self._current_account_id = ""
        self._session_data: dict = {}
        self._request_lock = threading.RLock()
        self._login_lock = threading.Lock()
        self._last_request_at = 0.0

    def login(self) -> None:
        with self._login_lock:
            if self._cst and time.monotonic() - self._session_started < 8 * 60:
                return
            response = request_json(
                f"{self.base_url}/api/v1/session",
                method="POST",
                headers={
                    "X-CAP-API-KEY": self.api_key,
                    "Content-Type": "application/json",
                },
                payload={
                    "identifier": self.identifier,
                    "password": self.password,
                    "encryptedPassword": False,
                },
                timeout=20,
            )
            self._cst = response.headers.get("cst", "")
            self._security_token = response.headers.get("x-security-token", "")
            if not self._cst or not self._security_token:
                raise RuntimeError("Capital.com login did not return session tokens.")
            self._session_started = time.monotonic()
            self._session_data = response.data if isinstance(response.data, dict) else {}
            self._streaming_host = str(
                self._session_data.get("streamingHost")
                or self._session_data.get("streamEndpoint")
                or DEFAULT_STREAM_URL
            )
            self._current_account_id = str(self._session_data.get("currentAccountId") or "")

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
        payload: dict | None = None,
        before_send: Callable[[], None] | None = None,
    ):
        with self._request_lock:
            wait = 0.11 - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
            query = f"?{urlencode(params)}" if params else ""
            url = f"{self.base_url}/api/v1{path}{query}"
            headers = self._headers()
            if before_send is not None:
                before_send()
            return request_json(
                url,
                method=method,
                headers=headers,
                payload=payload,
            ).data

    def stream_credentials(self) -> tuple[str, str, str]:
        with self._request_lock:
            self._headers()
            host = self._streaming_host or DEFAULT_STREAM_URL
            endpoint = host.rstrip("/")
            if not endpoint.endswith("/connect"):
                endpoint += "/connect"
            return self._cst, self._security_token, endpoint

    def all_markets(self) -> list[Market]:
        """Return the markets visible to the active account, not just FX pairs."""
        payload = self._request("GET", "/markets")
        rows = payload.get("markets", []) if isinstance(payload, dict) else []
        markets: list[Market] = []
        for row in rows if isinstance(rows, list) else []:
            market = Market.from_payload(row)
            if market.epic:
                markets.append(market)
        return markets

    def market_details(self, epic: str) -> dict:
        return self._request("GET", f"/markets/{quote(epic, safe='')}")

    def historical_prices(
        self,
        epic: str,
        resolution: str,
        maximum: int = 1000,
        *,
        from_time: str = "",
        to_time: str = "",
    ) -> list[dict]:
        if resolution not in SUPPORTED_RESOLUTIONS:
            raise ValueError(f"Unsupported Capital.com resolution: {resolution}")
        if not 1 <= maximum <= 1000:
            raise ValueError("Historical price limit must be between 1 and 1000.")
        params = {"resolution": resolution, "max": str(maximum)}
        if from_time:
            params["from"] = from_time
        if to_time:
            params["to"] = to_time
        payload = self._request(
            "GET",
            f"/prices/{quote(epic, safe='')}",
            params=params,
        )
        rows = payload.get("prices", []) if isinstance(payload, dict) else []
        return rows if isinstance(rows, list) else []

    def historical_prices_all(
        self,
        epic: str,
        resolution: str,
        maximum: int = 1000,
        page_limit: int = 100,
    ) -> tuple[list[dict], bool]:
        """Fetch older pages up to a safety cap, reporting whether pagination ended."""
        if not 1 <= page_limit <= 100:
            raise ValueError("Historical page limit must be between 1 and 100.")
        first_page = self.historical_prices(epic, resolution, maximum)
        collected: dict[str, dict] = {}

        def stamp(row: dict) -> str:
            return str(row.get("snapshotTimeUTC") or row.get("snapshotTime") or "")

        current_page = first_page
        for row in first_page:
            key = stamp(row)
            if key:
                collected[key] = row
        complete = len(first_page) < maximum

        for _ in range(1, page_limit):
            if complete:
                break
            timestamps = [stamp(row) for row in current_page if stamp(row)]
            if not timestamps:
                complete = False
                break
            oldest = min(timestamps)
            older_page = self.historical_prices(
                epic, resolution, maximum, to_time=oldest[:19]
            )
            older_rows = [
                row for row in older_page
                if stamp(row) and stamp(row) < oldest
            ]
            previous_count = len(collected)
            for row in older_rows:
                collected[stamp(row)] = row
            if not older_page:
                complete = True
                break
            if len(collected) == previous_count:
                # Do not claim complete history if the endpoint did not move back.
                complete = False
                break
            if len(older_page) < maximum:
                complete = True
                break
            current_page = older_rows

        ordered = [collected[key] for key in sorted(collected)]
        return ordered, complete

    def positions(self) -> list[dict]:
        payload = self._request("GET", "/positions")
        rows = payload.get("positions", []) if isinstance(payload, dict) else []
        return rows if isinstance(rows, list) else []

    def account_balance(self) -> tuple[float, str]:
        payload = self._request("GET", "/accounts")
        rows = payload.get("accounts", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise RuntimeError("Capital.com account list was not available.")
        account = next(
            (row for row in rows if str(row.get("accountId", "")) == self._current_account_id),
            None,
        ) or next((row for row in rows if row.get("preferred")), None)
        if not account:
            raise RuntimeError("Capital.com did not identify the active account.")
        balance = (account.get("balance") or {}).get("balance")
        try:
            amount = float(balance)
        except (TypeError, ValueError):
            raise RuntimeError("Capital.com account balance was unavailable.") from None
        return amount, str(account.get("currency") or "").upper()

    def create_position(
        self,
        *,
        epic: str,
        direction: str,
        size: float,
        stop_level: float,
        profit_level: float,
        product: str,
        before_submit: Callable[[], None] | None = None,
    ) -> dict:
        """Submit one order only when its mode is explicitly enabled and product is CFD."""
        enabled = (
            self.settings.enable_demo_orders
            if self.environment == "demo"
            else self.settings.live_trading_enabled
            and self.settings.live_trading_ack == LIVE_ACK_TEXT
        )
        if not enabled:
            raise RuntimeError(f"{self.environment.title()} broker orders are locked.")
        if product.strip().upper() != "CFD":
            raise RuntimeError("Order blocked: live product metadata did not confirm CFD.")
        if direction not in {"BUY", "SELL"} or not epic:
            raise ValueError("A valid epic and BUY/SELL direction are required.")
        if size <= 0 or stop_level <= 0 or profit_level <= 0:
            raise ValueError("Order size, stop, and profit levels must be positive.")
        payload = {
            "epic": epic,
            "direction": direction,
            "size": float(size),
            "guaranteedStop": False,
            "stopLevel": float(stop_level),
            "profitLevel": float(profit_level),
        }
        result = self._request(
            "POST", "/positions", payload=payload, before_send=before_submit
        )
        reference = str(result.get("dealReference") or "") if isinstance(result, dict) else ""
        if not reference:
            raise RuntimeError("Capital.com returned no deal reference; inspect the account before retrying.")
        # Capital.com notes that HTTP success alone is not proof of an opened position.
        # If confirmation fails, do not retry the POST because the first order may exist.
        confirmation = self._request("GET", f"/confirms/{quote(reference, safe='')}")
        if not isinstance(confirmation, dict):
            raise RuntimeError("Order confirmation was unavailable; inspect the account before retrying.")
        return confirmation


class CapitalDemoClient(CapitalClient):
    """Backward-compatible demo-only read client."""

    def __init__(self, settings: Settings):
        super().__init__(settings, environment="demo")
