from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


LIVE_ACK_TEXT = "I_UNDERSTAND_LIVE_CFD_RISK"


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
    trading_mode: str
    capital_api_key: str
    capital_identifier: str
    capital_password: str
    enable_demo_orders: bool
    live_trading_enabled: bool
    live_trading_ack: str
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
    paper_starting_balance: float
    paper_account_currency: str
    paper_max_leverage: float
    paper_risk_fraction: float
    paper_daily_loss_limit_fraction: float
    max_epic_exposure_fraction: float
    journal_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        allowed = frozenset(
            chat.strip()
            for chat in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
            if chat.strip()
        )
        result = cls(
            trading_mode=os.getenv("TRADING_MODE", "paper").strip().lower(),
            capital_api_key=os.getenv("CAPITAL_API_KEY", "").strip(),
            capital_identifier=os.getenv("CAPITAL_IDENTIFIER", "").strip(),
            capital_password=os.getenv("CAPITAL_PASSWORD", "").strip(),
            enable_demo_orders=_bool("ENABLE_DEMO_ORDERS", False),
            live_trading_enabled=_bool("LIVE_TRADING_ENABLED", False),
            live_trading_ack=os.getenv("LIVE_TRADING_ACK", "").strip(),
            finnhub_api_key=os.getenv("FINNHUB_API_KEY", "").strip(),
            fmp_api_key=os.getenv("FMP_API_KEY", "").strip(),
            fred_api_key=os.getenv("FRED_API_KEY", "").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            telegram_allowed_chat_ids=allowed,
            telegram_verbose_steps=_bool("TELEGRAM_VERBOSE_STEPS", True),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 60),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 1),
            max_daily_demo_orders=_int("MAX_DAILY_DEMO_ORDERS", 1),
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
            paper_starting_balance=_float("PAPER_STARTING_BALANCE", 10_000.0),
            paper_account_currency=os.getenv("PAPER_ACCOUNT_CURRENCY", "USD")
            .strip()
            .upper(),
            paper_max_leverage=_float("PAPER_MAX_LEVERAGE", 100.0),
            paper_risk_fraction=_float("PAPER_RISK_FRACTION", 0.01),
            paper_daily_loss_limit_fraction=_float(
                "PAPER_DAILY_LOSS_LIMIT_FRACTION", 0.03
            ),
            max_epic_exposure_fraction=_float(
                "MAX_EPIC_EXPOSURE_FRACTION", 0.25
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

    def capital_credentials_for(self, environment: str) -> tuple[str, str, str]:
        if environment not in {"demo", "live"}:
            raise ValueError("Capital.com environment must be demo or live.")
        return self.capital_api_key, self.capital_identifier, self.capital_password

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def validate(self) -> None:
        if self.trading_mode not in {"paper", "demo"}:
            raise ConfigurationError(
                "TRADING_MODE must start as paper or demo; Live can only be selected "
                "from an authorized Telegram chat after a second confirmation."
            )

        if self.live_trading_enabled and self.live_trading_ack != LIVE_ACK_TEXT:
            raise ConfigurationError(
                "LIVE_TRADING_ENABLED requires the exact LIVE_TRADING_ACK value."
            )
        if not self.live_trading_enabled and self.live_trading_ack:
            raise ConfigurationError(
                "LIVE_TRADING_ACK must stay empty unless LIVE_TRADING_ENABLED is true."
            )

        if not 1 <= self.max_open_positions <= 2:
            raise ConfigurationError("MAX_OPEN_POSITIONS is capped at 2.")
        if not 1 <= self.max_daily_demo_orders <= 5:
            raise ConfigurationError("MAX_DAILY_DEMO_ORDERS is capped at 5.")
        if not 15 <= self.poll_interval_seconds <= 3600:
            raise ConfigurationError("POLL_INTERVAL_SECONDS must be between 15 and 3600.")
        if not 0.5 <= self.min_sentiment_confidence <= 1:
            raise ConfigurationError("MIN_SENTIMENT_CONFIDENCE must be between 0.5 and 1.")
        if not 0.01 <= self.stop_distance_percent <= 2:
            raise ConfigurationError("STOP_DISTANCE_PERCENT must be between 0.01 and 2.")
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
        if self.paper_starting_balance <= 0:
            raise ConfigurationError("PAPER_STARTING_BALANCE must be greater than zero.")
        if len(self.paper_account_currency) != 3 or not self.paper_account_currency.isalpha():
            raise ConfigurationError("PAPER_ACCOUNT_CURRENCY must be a three-letter code.")
        if not 1 <= self.paper_max_leverage <= 100:
            raise ConfigurationError("PAPER_MAX_LEVERAGE must be between 1 and 100.")
        if not 0.001 <= self.paper_risk_fraction <= 0.02:
            raise ConfigurationError("PAPER_RISK_FRACTION must be between 0.001 and 0.02.")
        if not 0.005 <= self.paper_daily_loss_limit_fraction <= 0.10:
            raise ConfigurationError(
                "PAPER_DAILY_LOSS_LIMIT_FRACTION must be between 0.005 and 0.10."
            )
        if not 0.01 <= self.max_epic_exposure_fraction <= 1:
            raise ConfigurationError(
                "MAX_EPIC_EXPOSURE_FRACTION must be between 0.01 and 1."
            )

    def validate_for_run(self) -> None:
        self.validate()
        if not self.finnhub_api_key:
            raise ConfigurationError("FINNHUB_API_KEY is required to fetch financial headlines.")
        if self.telegram_bot_token and not self.telegram_chat_id:
            raise ConfigurationError("TELEGRAM_CHAT_ID is required when TELEGRAM_BOT_TOKEN is set.")
        if self.telegram_bot_token and not self.telegram_allowed_chat_ids:
            raise ConfigurationError(
                "Set TELEGRAM_ALLOWED_CHAT_IDS explicitly; control access is not inferred."
            )
