from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from finbertx.types import EconomicEvent, Market, NewsItem, SentimentScores, TradeSignal


CURRENCY_CODES = {
    "AED",
    "ARS",
    "AUD",
    "BHD",
    "BGN",
    "BRL",
    "CAD",
    "CHF",
    "CLP",
    "COP",
    "CNH",
    "CNY",
    "CZK",
    "DKK",
    "EGP",
    "EUR",
    "GBP",
    "HKD",
    "HUF",
    "IDR",
    "ILS",
    "INR",
    "ISK",
    "JPY",
    "KRW",
    "KWD",
    "MAD",
    "MXN",
    "MYR",
    "NOK",
    "NZD",
    "OMR",
    "PEN",
    "PHP",
    "PKR",
    "PLN",
    "QAR",
    "RON",
    "RUB",
    "SAR",
    "SEK",
    "SGD",
    "THB",
    "TRY",
    "TWD",
    "UAH",
    "USD",
    "ZAR",
}

CURRENCY_ALIASES = {
    "AED": ("united arab emirates", "uae", "emirati dirham"),
    "ARS": ("argentina", "argentine peso"),
    "AUD": ("australia", "australian", "aussie", "rba"),
    "BHD": ("bahrain", "bahraini dinar"),
    "BGN": ("bulgaria", "bulgarian lev"),
    "BRL": ("brazil", "brazilian real"),
    "CAD": ("canada", "canadian", "bank of canada"),
    "CHF": ("switzerland", "swiss franc", "swiss national bank", "snb"),
    "CLP": ("chile", "chilean peso"),
    "COP": ("colombia", "colombian peso"),
    "CNY": ("china", "chinese yuan", "renminbi", "pboc"),
    "EGP": ("egypt", "egyptian pound"),
    "EUR": ("eurozone", "euro area", "european central bank", "ecb", "euro"),
    "GBP": ("britain", "british pound", "sterling", "bank of england", "boe"),
    "IDR": ("indonesia", "indonesian rupiah"),
    "INR": ("india", "indian rupee"),
    "JPY": ("japan", "japanese yen", "bank of japan", "boj", "yen"),
    "KRW": ("south korea", "korean won", "bank of korea"),
    "KWD": ("kuwait", "kuwaiti dinar"),
    "MAD": ("morocco", "moroccan dirham"),
    "MXN": ("mexico", "mexican peso"),
    "MYR": ("malaysia", "malaysian ringgit"),
    "NOK": ("norway", "norwegian krone"),
    "NZD": ("new zealand", "kiwi", "reserve bank of new zealand", "rbnz"),
    "OMR": ("oman", "omani rial"),
    "PEN": ("peru", "peruvian sol"),
    "PHP": ("philippines", "philippine peso"),
    "PKR": ("pakistan", "pakistani rupee"),
    "QAR": ("qatar", "qatari riyal"),
    "RUB": ("russia", "russian ruble", "bank of russia"),
    "SAR": ("saudi arabia", "saudi riyal", "sama"),
    "SEK": ("sweden", "swedish krona", "riksbank"),
    "SGD": ("singapore", "singapore dollar"),
    "THB": ("thailand", "thai baht", "bank of thailand"),
    "TRY": ("turkey", "turkish lira"),
    "TWD": ("taiwan", "taiwan dollar"),
    "UAH": ("ukraine", "ukrainian hryvnia"),
    "USD": (
        "united states",
        "u.s.",
        "us economy",
        "federal reserve",
        "fed ",
        "greenback",
        "u.s. dollar",
        "us dollar",
    ),
}


def currency_pair(market: Market) -> tuple[str, str] | None:
    if market.instrument_type not in {"CURRENCIES", "CURRENCY", "FOREX", "FX"}:
        return None
    for source in (market.epic, market.name):
        upper = source.upper()
        token_pair = re.search(
            r"(?<![A-Z])([A-Z]{3})([A-Z]{3})(?![A-Z])", upper
        )
        if token_pair:
            base, quote = token_pair.groups()
            if base in CURRENCY_CODES and quote in CURRENCY_CODES:
                return base, quote

        compact = re.sub(r"[^A-Z]", "", upper)
        if len(compact) == 6:
            base, quote = compact[:3], compact[3:]
            if base in CURRENCY_CODES and quote in CURRENCY_CODES:
                return base, quote
        separated = re.search(r"\b([A-Z]{3})\s*[/_-]\s*([A-Z]{3})\b", upper)
        if separated:
            base, quote = separated.groups()
            if base in CURRENCY_CODES and quote in CURRENCY_CODES:
                return base, quote
    return None


def currencies_mentioned(text: str) -> set[str]:
    upper = text.upper()
    lowered = text.lower()
    found: set[str] = set()
    for code in CURRENCY_CODES:
        if re.search(rf"(?<![A-Z]){re.escape(code)}(?![A-Z])", upper):
            found.add(code)
    for code, aliases in CURRENCY_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            found.add(code)
    return found


def make_signal(
    news: NewsItem,
    sentiment: SentimentScores,
    market: Market,
    *,
    min_confidence: float,
    min_margin: float = 0.25,
) -> TradeSignal | None:
    pair = currency_pair(market)
    if pair is None or sentiment.confidence < min_confidence:
        return None
    if sentiment.directional_margin < min_margin:
        return None

    base, quote = pair
    combined = f"{news.headline} {news.summary} {news.related}"
    mentioned = currencies_mentioned(combined)
    mentioned_pair = f"{base}{quote}" in re.sub(r"[^A-Z]", "", combined.upper())
    if mentioned_pair:
        affected_currency = base
        direct_pair_signal = True
    elif (base in mentioned) ^ (quote in mentioned):
        affected_currency = base if base in mentioned else quote
        direct_pair_signal = False
    else:
        # No unique relationship to the pair; do not infer a trade from generic news.
        return None

    positive = sentiment.positive >= sentiment.negative
    if direct_pair_signal:
        direction = "BUY" if positive else "SELL"
    elif affected_currency == base:
        direction = "BUY" if positive else "SELL"
    else:
        direction = "SELL" if positive else "BUY"

    return TradeSignal(
        epic=market.epic,
        market_name=market.name,
        direction=direction,
        currency_basis=affected_currency,
        sentiment=sentiment,
    )


def is_in_high_impact_blackout(
    events: list[EconomicEvent],
    pair: tuple[str, str],
    *,
    now: datetime | None = None,
    before_minutes: int = 15,
    after_minutes: int = 10,
) -> EconomicEvent | None:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    start = moment - timedelta(minutes=after_minutes)
    end = moment + timedelta(minutes=before_minutes)
    for event in events:
        if not event.is_high_impact or event.currency not in pair:
            continue
        event_time = (
            event.at.replace(tzinfo=UTC) if event.at.tzinfo is None else event.at
        )
        if event.all_day:
            if event_time.astimezone(UTC).date() == moment.astimezone(UTC).date():
                return event
            continue
        if start <= event_time.astimezone(UTC) <= end:
            return event
    return None
