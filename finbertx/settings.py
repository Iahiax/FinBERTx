from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    pass


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Load simple KEY=VALUE lines without overriding process environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    capital_env: str
    capital_api_key: str
    capital_identifier: str
    capital_password: str
    enable_demo_orders: bool
    finnhub_api_key: str
    fmp_api_key: str
    fred_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_allowed_chat_ids: frozenset[str]
    telegram_verbose_steps: bool
    poll_interval_seconds: int
    max_open_positions: int
    max_daily_demo_orders: int
    min_sentiment_confidence: float
    stop_distance_percent: float
    take_profit_multiple: float
    high_impact_blackout_before_minutes: int
    high_impact_blackout_after_minutes: int
    max_news_per_cycle: int
    backtest_csv: str
    backtest_report_interval_hours: int
    journal_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        output_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        allowed = {
            chat.strip()
            for chat in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
            if chat.strip()
        }
        if not allowed and output_chat:
            allowed = {output_chat}

        result = cls(
            capital_env=os.getenv("CAPITAL_ENV", "demo").strip().lower(),
            capital_api_key=os.getenv("CAPITAL_API_KEY", "").strip(),
            capital_identifier=os.getenv("CAPITAL_IDENTIFIER", "").strip(),
            capital_password=os.getenv("CAPITAL_PASSWORD", "").strip(),
            enable_demo_orders=_bool("ENABLE_DEMO_ORDERS", False),
            finnhub_api_key=os.getenv("FINNHUB_API_KEY", "").strip(),
            fmp_api_key=os.getenv("FMP_API_KEY", "").strip(),
            fred_api_key=os.getenv("FRED_API_KEY", "").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=output_chat,
            telegram_allowed_chat_ids=frozenset(allowed),
            telegram_verbose_steps=_bool("TELEGRAM_VERBOSE_STEPS", True),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 60),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 2),
            max_daily_demo_orders=_int("MAX_DAILY_DEMO_ORDERS", 5),
            min_sentiment_confidence=_float("MIN_SENTIMENT_CONFIDENCE", 0.85),
            stop_distance_percent=_float("STOP_DISTANCE_PERCENT", 0.5),
            take_profit_multiple=_float("TAKE_PROFIT_MULTIPLE", 2.0),
            high_impact_blackout_before_minutes=_int(
                "HIGH_IMPACT_BLACKOUT_BEFORE_MINUTES", 15
            ),
            high_impact_blackout_after_minutes=_int(
                "HIGH_IMPACT_BLACKOUT_AFTER_MINUTES", 10
            ),
            max_news_per_cycle=_int("MAX_NEWS_PER_CYCLE", 10),
            backtest_csv=os.getenv("BACKTEST_CSV", "").strip(),
            backtest_report_interval_hours=_int(
                "BACKTEST_REPORT_INTERVAL_HOURS", 24
            ),
            journal_path=os.getenv("JOURNAL_PATH", "data/finbertx.sqlite3").strip(),
        )
        result.validate()
        return result

    @property
    def capital_configured(self) -> bool:
        return bool(
            self.capital_api_key and self.capital_identifier and self.capital_password
        )

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def validate(self) -> None:
        if self.capital_env != "demo":
            raise ConfigurationError(
                "Only Capital.com demo mode is supported; live mode is blocked."
            )
        if self.enable_demo_orders:
            raise ConfigurationError(
                "Order submission is intentionally unsupported. Set "
                "ENABLE_DEMO_ORDERS=false; this project is read-only and simulated."
            )
        if not 1 <= self.max_open_positions <= 2:
            raise ConfigurationError("MAX_OPEN_POSITIONS is capped at 2.")
        if not 1 <= self.max_daily_demo_orders <= 5:
            raise ConfigurationError("MAX_DAILY_DEMO_ORDERS is capped at 5.")
        if not 15 <= self.poll_interval_seconds <= 3600:
            raise ConfigurationError(
                "POLL_INTERVAL_SECONDS must be between 15 and 3600."
            )
        if not 0.5 <= self.min_sentiment_confidence <= 1:
            raise ConfigurationError(
                "MIN_SENTIMENT_CONFIDENCE must be between 0.5 and 1."
            )
        if not 0.01 <= self.stop_distance_percent <= 2:
            raise ConfigurationError(
                "STOP_DISTANCE_PERCENT must be between 0.01 and 2."
            )
        if not 0 <= self.high_impact_blackout_before_minutes <= 1440:
            raise ConfigurationError(
                "HIGH_IMPACT_BLACKOUT_BEFORE_MINUTES must be between 0 and 1440."
            )
        if not 0 <= self.high_impact_blackout_after_minutes <= 1440:
            raise ConfigurationError(
                "HIGH_IMPACT_BLACKOUT_AFTER_MINUTES must be between 0 and 1440."
            )
        if not 1 <= self.take_profit_multiple <= 3:
            raise ConfigurationError("TAKE_PROFIT_MULTIPLE must be between 1 and 3.")
        if not 1 <= self.max_news_per_cycle <= 50:
            raise ConfigurationError("MAX_NEWS_PER_CYCLE must be between 1 and 50.")
        if self.backtest_report_interval_hours < 1:
            raise ConfigurationError(
                "BACKTEST_REPORT_INTERVAL_HOURS must be at least 1."
            )


    def validate_for_run(self) -> None:
        self.validate()
        if not self.finnhub_api_key:
            raise ConfigurationError(
                "FINNHUB_API_KEY is required to fetch financial headlines."
            )
        if self.telegram_bot_token and not self.telegram_chat_id:
            raise ConfigurationError(
                "TELEGRAM_CHAT_ID is required when TELEGRAM_BOT_TOKEN is set."
            )
        if self.enable_demo_orders and not (
            self.finnhub_api_key or self.fmp_api_key or self.fred_api_key
        ):
            raise ConfigurationError("No economic calendar provider is configured.")
