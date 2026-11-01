from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SentimentScores:
    positive: float
    negative: float
    neutral: float

    @property
    def label(self) -> str:
        values = {
            "positive": self.positive,
            "negative": self.negative,
            "neutral": self.neutral,
        }
        return max(values, key=values.get)

    @property
    def confidence(self) -> float:
        return max(self.positive, self.negative)

    @property
    def directional_margin(self) -> float:
        return abs(self.positive - self.negative)


@dataclass(frozen=True)
class NewsItem:
    news_id: str
    headline: str
    summary: str = ""
    related: str = ""
    published_at: datetime | None = None


@dataclass(frozen=True)
class EconomicEvent:
    name: str
    currency: str
    impact: str
    at: datetime
    all_day: bool = False

    @property
    def is_high_impact(self) -> bool:
        return self.impact.strip().lower() in {
            "high",
            "3",
            "3.0",
            "high impact",
        }


@dataclass(frozen=True)
class Market:
    epic: str
    name: str
    instrument_type: str
    market_status: str
    bid: float | None = None
    offer: float | None = None
    decimal_places: int = 5
    min_deal_size: float = 0.0
    max_deal_size: float = 0.0
    min_stop_percent: float = 0.0
    max_stop_percent: float = 100.0

    @classmethod
    def from_payload(cls, payload: dict) -> "Market":
        instrument = payload.get("instrument") or {}
        snapshot = payload.get("snapshot") or {}
        rules = payload.get("dealingRules") or {}

        def rule_value(name: str, default: float) -> float:
            value = (rules.get(name) or {}).get("value", default)
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        decimals = snapshot.get("decimalPlacesFactor", 5)
        try:
            decimals = max(0, min(10, int(decimals)))
        except (TypeError, ValueError):
            decimals = 5

        return cls(
            epic=str(payload.get("epic") or instrument.get("epic") or ""),
            name=str(
                payload.get("instrumentName")
                or payload.get("symbol")
                or instrument.get("name")
                or ""
            ),
            instrument_type=str(
                payload.get("instrumentType") or instrument.get("type") or ""
            ).upper(),
            market_status=str(
                payload.get("marketStatus") or snapshot.get("marketStatus") or ""
            ).upper(),
            bid=_to_float(payload.get("bid", snapshot.get("bid"))),
            offer=_to_float(payload.get("offer", snapshot.get("offer"))),
            decimal_places=decimals,
            min_deal_size=rule_value("minDealSize", 0.0),
            max_deal_size=rule_value("maxDealSize", 0.0),
            min_stop_percent=rule_value("minStopOrProfitDistance", 0.0),
            max_stop_percent=rule_value("maxStopOrProfitDistance", 100.0),
        )


@dataclass(frozen=True)
class TradeSignal:
    epic: str
    market_name: str
    direction: str
    currency_basis: str
    sentiment: SentimentScores


def _to_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
