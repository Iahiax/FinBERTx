from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

from finbertx.network import ApiError, request_json
from finbertx.types import EconomicEvent, NewsItem


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except ValueError:
            continue
    return None


class FinnhubSource:
    base_url = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(self, path: str, params: dict[str, str] | None = None):
        query = dict(params or {})
        query["token"] = self.api_key
        url = f"{self.base_url}{path}?{urlencode(query)}"
        return request_json(url).data

    def forex_news(self, limit: int = 10) -> list[NewsItem]:
        payload = self._get("/news", {"category": "forex"})
        if not isinstance(payload, list):
            raise ApiError("Finnhub returned an unexpected news response.")
        items: list[NewsItem] = []
        for row in payload[:limit]:
            headline = str(row.get("headline") or "").strip()
            if not headline:
                continue
            published_at = _parse_time(row.get("datetime"))
            identifier = str(row.get("id") or "")
            if not identifier:
                raw_id = f"{headline}|{row.get('datetime', '')}|{row.get('url', '')}"
                identifier = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()
            items.append(
                NewsItem(
                    news_id=identifier,
                    headline=headline,
                    summary=str(row.get("summary") or ""),
                    related=str(row.get("related") or ""),
                    published_at=published_at,
                )
            )
        return items

    def economic_events(self, start: date, end: date) -> list[EconomicEvent]:
        payload = self._get(
            "/calendar/economic",
            {"from": start.isoformat(), "to": end.isoformat()},
        )
        rows = payload.get("economicCalendar", []) if isinstance(payload, dict) else []
        events: list[EconomicEvent] = []
        for row in rows:
            at = _parse_time(row.get("time"))
            if at is None:
                continue
            events.append(
                EconomicEvent(
                    name=str(row.get("event") or "Economic event"),
                    currency=str(row.get("currency") or "").upper(),
                    impact=str(row.get("impact") or ""),
                    at=at,
                )
            )
        return events


class FmpCalendar:
    base_url = "https://financialmodelingprep.com/stable/economic-calendar"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def economic_events(self, start: date, end: date) -> list[EconomicEvent]:
        query = urlencode(
            {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "apikey": self.api_key,
            }
        )
        payload = request_json(f"{self.base_url}?{query}").data
        if not isinstance(payload, list):
            raise ApiError("FMP returned an unexpected calendar response.")
        return _events_from_rows(payload)


class FredCalendar:
    base_url = "https://api.stlouisfed.org/fred/releases/dates"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def economic_events(self, start: date, end: date) -> list[EconomicEvent]:
        query = urlencode(
            {
                "api_key": self.api_key,
                "file_type": "json",
                "realtime_start": start.isoformat(),
                "realtime_end": end.isoformat(),
                "include_release_dates_with_no_data": "true",
            }
        )
        payload = request_json(f"{self.base_url}?{query}").data
        rows = payload.get("release_dates", []) if isinstance(payload, dict) else []
        events: list[EconomicEvent] = []
        for row in rows:
            release_day = _parse_time(row.get("date"))
            if release_day is None:
                continue
            title = str(row.get("release_name") or "FRED release")
            events.append(
                EconomicEvent(
                    name=title,
                    currency="USD",
                    impact=_infer_impact(title),
                    at=release_day,
                    all_day=True,
                )
            )
        return events


class EconomicCalendar:
    def __init__(
        self,
        *,
        finnhub_key: str = "",
        fmp_key: str = "",
        fred_key: str = "",
    ):
        self.providers = []
        if finnhub_key:
            self.providers.append(FinnhubSource(finnhub_key))
        if fmp_key:
            self.providers.append(FmpCalendar(fmp_key))
        if fred_key:
            self.providers.append(FredCalendar(fred_key))
        self.last_fetch_succeeded = False
        self._cached_events: list[EconomicEvent] = []
        self._cached_at = 0.0

    def upcoming(self, days: int = 2) -> list[EconomicEvent]:
        import time

        if self._cached_at and time.monotonic() - self._cached_at < 900:
            return list(self._cached_events)
        if not self.providers:
            self.last_fetch_succeeded = False
            return []
        start = datetime.now(UTC).date()
        end = start + timedelta(days=days)
        events: list[EconomicEvent] = []
        succeeded = False
        failures = 0
        for provider in self.providers:
            try:
                events.extend(provider.economic_events(start, end))
                succeeded = True
            except ApiError:
                failures += 1
        self.last_fetch_succeeded = succeeded
        if failures == len(self.providers):
            raise ApiError("All configured economic calendar providers failed.")
        deduplicated: dict[tuple[str, str, str], EconomicEvent] = {}
        for event in events:
            key = (event.name.casefold(), event.currency, event.at.isoformat())
            deduplicated[key] = event
        self._cached_events = sorted(deduplicated.values(), key=lambda event: event.at)
        self._cached_at = time.monotonic()
        return list(self._cached_events)


def _events_from_rows(rows: list[dict]) -> list[EconomicEvent]:
    events: list[EconomicEvent] = []
    for row in rows:
        at = _parse_time(row.get("date") or row.get("time"))
        if at is None:
            continue
        events.append(
            EconomicEvent(
                name=str(row.get("event") or row.get("name") or "Economic event"),
                currency=str(row.get("currency") or "").upper(),
                impact=str(row.get("impact") or ""),
                at=at,
            )
        )
    return events


def _infer_impact(title: str) -> str:
    important_terms = (
        "interest rate",
        "monetary policy",
        "federal funds",
        "employment",
        "unemployment",
        "consumer price",
        "inflation",
        "gross domestic product",
        "gdp",
        "nonfarm",
    )
    lowered = title.lower()
    return "high" if any(term in lowered for term in important_terms) else "unknown"
