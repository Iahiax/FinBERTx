from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from finbertx.backtest import run_backtest
from finbertx.capital import SUPPORTED_RESOLUTIONS
from finbertx.journal import Journal
from finbertx.settings import ConfigurationError, Settings
from finbertx.strategy import (
    currency_pair,
    is_in_high_impact_blackout,
    make_signal,
)
from finbertx.types import EconomicEvent, Market, NewsItem, SentimentScores


class StrategyTests(unittest.TestCase):
    def test_currency_market_is_detected(self):
        market = Market(
            epic="EURUSD",
            name="EUR/USD",
            instrument_type="CURRENCIES",
            market_status="TRADEABLE",
        )
        self.assertEqual(currency_pair(market), ("EUR", "USD"))

    def test_signal_requires_unique_currency_context(self):
        market = Market(
            epic="EURUSD",
            name="Euro vs US Dollar",
            instrument_type="CURRENCIES",
            market_status="TRADEABLE",
        )
        scores = SentimentScores(positive=0.92, negative=0.03, neutral=0.05)
        headline = NewsItem("n1", "European Central Bank signals stronger euro")
        signal = make_signal(
            headline, scores, market, min_confidence=0.85, min_margin=0.25
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "BUY")

        ambiguous = NewsItem("n2", "Markets rise after the latest policy news")
        self.assertIsNone(
            make_signal(ambiguous, scores, market, min_confidence=0.85)
        )

    def test_high_impact_event_blocks_nearby_trade(self):
        now = datetime.now(UTC)
        event = EconomicEvent(
            "Rate decision",
            "USD",
            "high",
            now + timedelta(minutes=5),
        )
        self.assertIsNotNone(
            is_in_high_impact_blackout([event], ("EUR", "USD"), now=now)
        )
        self.assertIsNone(
            is_in_high_impact_blackout([event], ("EUR", "GBP"), now=now)
        )

    def test_date_only_high_impact_event_blocks_the_full_utc_day(self):
        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        event = EconomicEvent(
            "US employment release",
            "USD",
            "high",
            now.replace(hour=0),
            all_day=True,
        )
        self.assertIsNotNone(
            is_in_high_impact_blackout([event], ("EUR", "USD"), now=now)
        )

    def test_all_capital_price_resolutions_are_available(self):
        self.assertEqual(
            SUPPORTED_RESOLUTIONS,
            (
                "MINUTE",
                "MINUTE_5",
                "MINUTE_15",
                "MINUTE_30",
                "HOUR",
                "HOUR_4",
                "DAY",
                "WEEK",
            ),
        )


class SettingsTests(unittest.TestCase):
    def test_live_mode_is_rejected(self):
        with patch.dict(os.environ, {"CAPITAL_ENV": "live"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_safety_limits_cannot_be_raised(self):
        with patch.dict(
            os.environ,
            {"MAX_OPEN_POSITIONS": "3", "MAX_DAILY_DEMO_ORDERS": "6"},
            clear=True,
        ):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_demo_ordering_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.enable_demo_orders)
        self.assertEqual(settings.capital_env, "demo")

    def test_order_submission_flag_is_rejected(self):
        with patch.dict(os.environ, {"ENABLE_DEMO_ORDERS": "true"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()


class JournalTests(unittest.TestCase):
    def test_news_is_deduplicated_and_pause_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(str(Path(tmp) / "journal.sqlite3"))
            self.assertTrue(journal.mark_news_seen("news-1"))
            self.assertFalse(journal.mark_news_seen("news-1"))
            journal.set_paused(True)
            self.assertTrue(journal.paused)


class BacktestTests(unittest.TestCase):
    def test_backtest_calculates_short_and_long_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.csv"
            path.write_text(
                "timestamp,epic,direction,entry_price,exit_price\n"
                "2026-01-01T00:00:00Z,EURUSD,BUY,1.00,1.10\n"
                "2026-01-02T00:00:00Z,EURUSD,SELL,1.00,1.05\n",
                encoding="utf-8",
            )
            report = run_backtest(path)
        self.assertEqual(report.trades, 2)
        self.assertEqual(report.wins, 1)
        self.assertEqual(report.losses, 1)
        self.assertAlmostEqual(report.net_return_percent, 4.5)


if __name__ == "__main__":
    unittest.main()
