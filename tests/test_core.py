from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from finbertx.backtest import run_backtest
from finbertx.capital import (
    DEMO_BASE_URL,
    LIVE_BASE_URL,
    CapitalClient,
    SUPPORTED_RESOLUTIONS,
)
from finbertx.journal import Journal
from finbertx.multiframe_backtest import backtest_timeframe, report_text, run_all_timeframes
from finbertx.network import JsonResponse
from finbertx.paper import PaperTradingEngine
from finbertx.runner import TradingBot, calculate_broker_order
from finbertx.settings import ConfigurationError, LIVE_ACK_TEXT, Settings
from finbertx.sources import FinnhubSource
from finbertx.strategy import (
    currency_pair,
    is_in_high_impact_blackout,
    make_signal,
)
from finbertx.types import EconomicEvent, Market, NewsItem, SentimentScores
from finbertx.streaming import CapitalWebSocketStream, StreamCache, chunks, subscription_message


class StrategyTests(unittest.TestCase):
    def test_currency_market_is_detected(self):
        market = Market("EURUSD", "EUR/USD", "CURRENCIES", "TRADEABLE")
        self.assertEqual(currency_pair(market), ("EUR", "USD"))

    def test_signal_requires_unique_currency_context(self):
        market = Market("EURUSD", "Euro vs US Dollar", "CURRENCIES", "TRADEABLE")
        scores = SentimentScores(positive=0.92, negative=0.03, neutral=0.05)
        headline = NewsItem("n1", "European Central Bank signals stronger euro")
        signal = make_signal(headline, scores, market, min_confidence=0.85)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "BUY")
        ambiguous = NewsItem("n2", "Markets rise after the latest policy news")
        self.assertIsNone(make_signal(ambiguous, scores, market, min_confidence=0.85))

    def test_high_impact_event_blocks_nearby_trade(self):
        now = datetime.now(UTC)
        event = EconomicEvent("Rate decision", "USD", "high", now + timedelta(minutes=5))
        self.assertIsNotNone(is_in_high_impact_blackout([event], ("EUR", "USD"), now=now))
        self.assertIsNone(is_in_high_impact_blackout([event], ("EUR", "GBP"), now=now))

    def test_date_only_high_impact_event_blocks_the_full_utc_day(self):
        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        event = EconomicEvent("US employment release", "USD", "high", now.replace(hour=0), all_day=True)
        self.assertIsNotNone(is_in_high_impact_blackout([event], ("EUR", "USD"), now=now))

    def test_all_capital_price_resolutions_are_available(self):
        self.assertEqual(
            SUPPORTED_RESOLUTIONS,
            ("MINUTE", "MINUTE_5", "MINUTE_15", "MINUTE_30", "HOUR", "HOUR_4", "DAY", "WEEK"),
        )

    def test_non_fx_signal_requires_explicit_ticker_or_name_match(self):
        market = Market("AAPL", "Apple Inc", "SHARES", "TRADEABLE", symbol="AAPL")
        scores = SentimentScores(0.93, 0.02, 0.05)
        self.assertIsNone(
            make_signal(NewsItem("n1", "Markets rally on policy hopes"), scores, market, min_confidence=0.85)
        )
        signal = make_signal(
            NewsItem("n2", "Apple Inc shares rise", related="AAPL"),
            scores,
            market,
            min_confidence=0.85,
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "BUY")


class EconomicCalendarTests(unittest.TestCase):
    def test_finnhub_country_code_maps_to_currency_without_network(self):
        source = FinnhubSource("local-test-placeholder")
        payload = {
            "economicCalendar": [
                {"country": "US", "event": "Inflation", "impact": "high", "time": "2026-01-01 12:00:00"}
            ]
        }
        with patch.object(source, "_get", return_value=payload):
            events = source.economic_events(date(2026, 1, 1), date(2026, 1, 2))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].currency, "USD")
        self.assertEqual(events[0].impact, "high")


class SettingsTests(unittest.TestCase):
    def test_paper_mode_is_default_and_live_is_locked(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.trading_mode, "paper")
        self.assertEqual(settings.paper_max_leverage, 100)
        self.assertEqual(settings.paper_daily_loss_limit_fraction, 0.03)
        self.assertEqual(settings.max_epic_exposure_fraction, 0.25)
        self.assertEqual(settings.max_open_positions, 1)
        self.assertEqual(settings.max_daily_demo_orders, 1)
        with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_live_cannot_be_selected_at_process_start(self):
        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "live",
                "LIVE_TRADING_ENABLED": "true",
                "LIVE_TRADING_ACK": LIVE_ACK_TEXT,
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigurationError, "Telegram"):
                Settings.from_env()

    def test_live_gate_can_be_staged_while_process_still_starts_in_paper(self):
        with patch.dict(
            os.environ,
            {"LIVE_TRADING_ENABLED": "true", "LIVE_TRADING_ACK": LIVE_ACK_TEXT},
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.trading_mode, "paper")
        self.assertTrue(settings.live_trading_enabled)

    def test_shared_capital_credentials_do_not_enable_live_by_themselves(self):
        with patch.dict(
            os.environ,
            {
                "CAPITAL_API_KEY": "key-placeholder",
                "CAPITAL_IDENTIFIER": "user-placeholder",
                "CAPITAL_PASSWORD": "password-placeholder",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertTrue(settings.capital_configured)
        self.assertFalse(settings.live_trading_enabled)
        CapitalClient(settings, environment="demo")
        with self.assertRaisesRegex(ValueError, "Live trading is locked"):
            CapitalClient(settings, environment="live")

    def test_demo_order_gate_is_disabled_by_default_and_explicitly_configurable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(Settings.from_env().enable_demo_orders)
        with patch.dict(os.environ, {"ENABLE_DEMO_ORDERS": "true"}, clear=True):
            self.assertTrue(Settings.from_env().enable_demo_orders)

    def test_safety_limits_cannot_be_raised(self):
        with patch.dict(os.environ, {"MAX_OPEN_POSITIONS": "3"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()
        with patch.dict(os.environ, {"PAPER_MAX_LEVERAGE": "101"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()
        with patch.dict(os.environ, {"PAPER_DAILY_LOSS_LIMIT_FRACTION": "0.2"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()
        with patch.dict(os.environ, {"MAX_EPIC_EXPOSURE_FRACTION": "1.1"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_telegram_command_allowlist_must_be_explicit(self):
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "local-test-placeholder", "TELEGRAM_BOT_TOKEN": "local-test-placeholder", "TELEGRAM_CHAT_ID": "channel-placeholder"}, clear=True):
            settings = Settings.from_env()
            with self.assertRaisesRegex(ConfigurationError, "TELEGRAM_ALLOWED_CHAT_IDS"):
                settings.validate_for_run()


class CapitalSessionTests(unittest.TestCase):
    def test_shared_credentials_select_mode_specific_hosts(self):
        env = {
            "CAPITAL_API_KEY": "demo-key-placeholder",
            "CAPITAL_IDENTIFIER": "demo-user-placeholder",
            "CAPITAL_PASSWORD": "demo-password-placeholder",
            "LIVE_TRADING_ENABLED": "true",
            "LIVE_TRADING_ACK": LIVE_ACK_TEXT,
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
        demo = CapitalClient(settings, environment="demo")
        live = CapitalClient(settings, environment="live")
        shared_credentials = (
            "demo-key-placeholder",
            "demo-user-placeholder",
            "demo-password-placeholder",
        )
        self.assertEqual(demo.base_url, DEMO_BASE_URL)
        self.assertEqual(live.base_url, LIVE_BASE_URL)
        self.assertEqual(settings.capital_credentials_for("demo"), shared_credentials)
        self.assertEqual(settings.capital_credentials_for("live"), shared_credentials)
        for client in (demo, live):
            self.assertEqual(
                (client.api_key, client.identifier, client.password),
                shared_credentials,
            )
        self.assertNotEqual(demo.base_url, live.base_url)

    def test_session_post_uses_json_payload_timeout_and_keeps_tokens_in_memory(self):
        env = {
            "CAPITAL_API_KEY": "key-placeholder",
            "CAPITAL_IDENTIFIER": "user-placeholder",
            "CAPITAL_PASSWORD": "password-placeholder",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
        client = CapitalClient(settings, environment="demo")
        response = JsonResponse(
            data={"streamingHost": "wss://stream.example.invalid/connect"},
            headers={"cst": "cst-placeholder", "x-security-token": "token-placeholder"},
        )
        with patch("finbertx.capital.request_json", return_value=response) as request:
            client.login()
        args, kwargs = request.call_args
        self.assertEqual(args[0], f"{DEMO_BASE_URL}/api/v1/session")
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["headers"], {
            "X-CAP-API-KEY": "key-placeholder",
            "Content-Type": "application/json",
        })
        self.assertEqual(kwargs["payload"], {
            "identifier": "user-placeholder",
            "password": "password-placeholder",
            "encryptedPassword": False,
        })
        self.assertEqual(kwargs["timeout"], 20)
        self.assertEqual(client._cst, "cst-placeholder")
        self.assertEqual(client._security_token, "token-placeholder")
        self.assertEqual(
            client.stream_credentials(),
            ("cst-placeholder", "token-placeholder", "wss://stream.example.invalid/connect"),
        )

    def test_pre_submit_guard_runs_after_auth_headers_and_immediately_before_post(self):
        with patch.dict(os.environ, {
            "CAPITAL_API_KEY": "key-placeholder",
            "CAPITAL_IDENTIFIER": "user-placeholder",
            "CAPITAL_PASSWORD": "password-placeholder",
        }, clear=True):
            client = CapitalClient(Settings.from_env(), environment="demo")
        order = []

        def headers():
            order.append("headers")
            return {"CST": "cst-placeholder"}

        def request(*args, **kwargs):
            order.append("post")
            return JsonResponse({}, {})

        with patch.object(client, "_headers", side_effect=headers), \
             patch("finbertx.capital.time.sleep"), \
             patch("finbertx.capital.request_json", side_effect=request):
            client._request(
                "POST", "/positions", payload={"epic": "TEST"},
                before_send=lambda: order.append("guard"),
            )
        self.assertEqual(order, ["headers", "guard", "post"])


class JournalTests(unittest.TestCase):
    def test_news_is_deduplicated_and_pause_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(str(Path(tmp) / "journal.sqlite3"))
            self.assertTrue(journal.mark_news_seen("news-1"))
            self.assertFalse(journal.mark_news_seen("news-1"))
            journal.set_paused(True)
            self.assertTrue(journal.paused)

    def test_broker_attempt_is_durable_and_counted_separately_from_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "journal.sqlite3")
            journal = Journal(path)
            self.assertEqual(journal.reserve_broker_attempt("EPIC1", "demo", 5), "reserved")
            journal.mark_broker_attempt_ambiguous("EPIC1", "TimeoutError")
            reopened = Journal(path)
            self.assertEqual(reopened.broker_attempt_status("EPIC1"), "AMBIGUOUS")
            self.assertEqual(reopened.daily_broker_order_count(), 1)
            self.assertEqual(reopened.confirmed_broker_order_count(), 0)
            self.assertEqual(reopened.reserve_broker_attempt("EPIC1", "live", 5), "locked")

    def test_broker_attempt_daily_limit_is_atomic_and_success_is_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(str(Path(tmp) / "journal.sqlite3"))
            self.assertEqual(journal.reserve_broker_attempt("EPIC1", "demo", 1), "reserved")
            self.assertEqual(journal.reserve_broker_attempt("EPIC2", "demo", 1), "daily_limit")
            journal.mark_broker_attempt_confirmed("EPIC1", "ACCEPTED")
            self.assertEqual(journal.daily_broker_order_count(), 1)
            self.assertEqual(journal.confirmed_broker_order_count(), 1)
            self.assertFalse(journal.broker_epic_locked("EPIC1"))
            self.assertEqual(journal.reserve_broker_attempt("EPIC1", "demo", 3), "reserved")
            self.assertEqual(journal.daily_broker_order_count(), 2)

    def test_restart_during_submission_converts_pending_attempt_to_ambiguous_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "journal.sqlite3")
            journal = Journal(path)
            self.assertEqual(journal.reserve_broker_attempt("EPIC1", "demo", 5), "reserved")
            restarted = Journal(path)
            self.assertEqual(restarted.broker_attempt_status("EPIC1"), "AMBIGUOUS")
            self.assertTrue(restarted.broker_epic_locked("EPIC1"))


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(str(Path(self.tmp.name) / "journal.sqlite3"))
        self.engine = PaperTradingEngine(
            self.journal,
            starting_balance=1_000,
            account_currency="USD",
            max_leverage=100,
            risk_fraction=0.01,
            stop_distance_percent=0.5,
            take_profit_multiple=2,
            max_open_positions=2,
            max_daily_trades=5,
            daily_loss_limit_fraction=0.03,
            max_epic_exposure_fraction=0.25,
        )
        self.market = Market(
            "EURUSD", "EUR/USD", "CURRENCIES", "TRADEABLE", bid=1.1000, offer=1.1002
        )
        self.signal = make_signal(
            NewsItem("news-1", "European Central Bank signals stronger euro"),
            SentimentScores(0.92, 0.03, 0.05),
            self.market,
            min_confidence=0.85,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_size_is_capped_and_based_on_current_accumulated_balance(self):
        first = self.engine.open_signal(self.signal, self.market, signal_id="s1")
        self.assertIsNotNone(first.fill)
        self.assertAlmostEqual(first.fill.notional, 250, places=2)
        self.assertAlmostEqual(first.fill.leverage, 0.25, places=5)
        target_market = Market(
            "EURUSD", "EUR/USD", "CURRENCIES", "TRADEABLE",
            bid=first.fill.target_price + 0.001, offer=first.fill.target_price + 0.0012,
        )
        closes = self.engine.update_positions({"EURUSD": target_market})
        self.assertEqual(len(closes), 1)
        self.assertGreater(closes[0].pnl, 0)
        self.assertGreater(closes[0].balance, 1_000)
        next_market = Market(
            "EURUSD", "EUR/USD", "CURRENCIES", "TRADEABLE", bid=1.1050, offer=1.1052
        )
        second = self.engine.open_signal(
            self.signal, next_market, signal_id="s2", markets={"EURUSD": next_market}
        )
        self.assertIsNotNone(second.fill)
        self.assertGreater(second.fill.notional, 250)
        self.assertLessEqual(second.fill.notional, 1_000.0 * 0.25 * 1.01)
        self.assertLessEqual(second.fill.leverage, 100)

    def test_closed_market_cannot_open_a_paper_position(self):
        closed = Market("EURUSD", "EUR/USD", "CURRENCIES", "CLOSED", bid=1.1, offer=1.1002)
        result = self.engine.open_signal(self.signal, closed, signal_id="closed-signal")
        self.assertIsNone(result.fill)
        self.assertEqual(len(self.journal.open_paper_positions()), 0)

    def test_maximum_leverage_is_a_ceiling_not_an_unbounded_value(self):
        engine = PaperTradingEngine(
            self.journal,
            starting_balance=1_000,
            account_currency="USD",
            max_leverage=100,
            risk_fraction=0.02,
            stop_distance_percent=0.01,
            take_profit_multiple=1,
            max_open_positions=2,
            max_daily_trades=5,
            daily_loss_limit_fraction=0.03,
            max_epic_exposure_fraction=0.25,
        )
        result = engine.open_signal(self.signal, self.market, signal_id="high-risk")
        self.assertIsNotNone(result.fill)
        self.assertLessEqual(result.fill.leverage, 100)
        self.assertLessEqual(result.fill.notional, 250)

    def test_daily_paper_loss_limit_blocks_new_positions_at_three_percent(self):
        today = datetime.now(UTC).date().isoformat()
        self.journal.set_value(f"paper_day_start_equity:{today}", "1000.0")
        self.journal.set_paper_balance(970.0)

        result = self.engine.open_signal(self.signal, self.market, signal_id="after-daily-loss")

        self.assertIsNone(result.fill)
        self.assertIn("حد الخسارة اليومية", result.reason)

    def test_per_epic_exposure_cap_limits_paper_notional(self):
        result = self.engine.open_signal(self.signal, self.market, signal_id="epic-cap")

        self.assertIsNotNone(result.fill)
        self.assertLessEqual(result.fill.notional, 250.0)

    def test_day_start_equity_is_persisted_for_restarts(self):
        today = datetime.now(UTC).date().isoformat()
        first = self.journal.paper_day_start_equity(today, 1000.0)
        second = self.journal.paper_day_start_equity(today, 900.0)

        self.assertEqual(first, 1000.0)
        self.assertEqual(second, 1000.0)


class TelegramInteractionTests(unittest.TestCase):
    def test_private_allowlisted_text_is_analyzed_without_network(self):
        class FakeTelegram:
            def __init__(self):
                self.sent = []

            def updates(self):
                return [
                    {"message": {"chat": {"id": 42, "type": "private"}, "text": "Inflation eased."}},
                    {"message": {"chat": {"id": 42, "type": "group"}, "text": "Ignore group."}},
                    {"message": {"chat": {"id": 99, "type": "private"}, "text": "Ignore user."}},
                ]

            def send(self, text, chat_id=None):
                self.sent.append((text, chat_id))

        class FakeEngine:
            def analyze(self, text):
                return SentimentScores(0.7, 0.1, 0.2)

        class FakeJournal:
            def add_event(self, *args, **kwargs):
                pass

        bot = TradingBot.__new__(TradingBot)
        bot.telegram = FakeTelegram()
        bot.settings = SimpleNamespace(telegram_allowed_chat_ids=frozenset({"42"}))
        bot.engine = FakeEngine()
        bot.journal = FakeJournal()
        bot._handle_telegram_updates()
        self.assertEqual(len(bot.telegram.sent), 1)
        self.assertEqual(bot.telegram.sent[0][1], "42")
        self.assertIn("Positive: 0.700", bot.telegram.sent[0][0])

    def test_backtest_telegram_command_requires_epic_and_never_returns_csv(self):
        bot = TradingBot.__new__(TradingBot)
        bot.capital = None
        bot._notify = Mock()

        bot._run_command("/backtest", "42", "")

        response = bot._notify.call_args.args[0]
        self.assertIn("الاستخدام: /backtest <EPIC>", response)
        self.assertIn("نصيًا", response)
        self.assertNotIn("CSV", response)

    def test_backtest_with_epic_without_capital_reports_unavailable_without_csv(self):
        bot = TradingBot.__new__(TradingBot)
        bot.capital = None
        bot._notify = Mock()

        bot._run_command("/backtest", "42", "EURUSD")

        response = bot._notify.call_args.args[0]
        self.assertIn("Capital.com غير مهيأة", response)
        self.assertNotIn("CSV", response)

    def test_live_mode_requires_a_chat_bound_second_confirmation(self):
        class FakeSettings:
            live_trading_enabled = True
            live_trading_ack = LIVE_ACK_TEXT
            capital_configured = True
            trading_mode = "paper"
            enable_demo_orders = False

        class FakeCapital:
            pass

        class FakeJournal:
            pass

        bot = TradingBot.__new__(TradingBot)
        bot.settings = FakeSettings()
        bot.active_mode = "paper"
        bot._mode_armed = False
        bot._pending_live_confirmations = {}
        bot._broker_clients = {"demo": FakeCapital()}
        bot.capital = bot._broker_clients["demo"]
        bot.market_stream = None
        bot.stream_cache = StreamCache()
        bot._last_multiframe_candidates = {}
        bot._market_cache = None
        bot.journal = FakeJournal()

        with patch("finbertx.runner.CapitalClient", return_value=FakeCapital()):
            first = bot._handle_mode_command("live", "42")
            self.assertIn("لم يتغير الوضع بعد", first)
            self.assertEqual(bot.active_mode, "paper")
            nonce = first.split("/mode confirm_live ", 1)[1].split()[0]
            second = bot._handle_mode_command(f"confirm_live {nonce}", "99")
            self.assertIn("لا يوجد تأكيد Live صالح", second)
            self.assertEqual(bot.active_mode, "paper")
            third = bot._handle_mode_command(f"confirm_live {nonce}", "42")
        self.assertIn("تم تأكيد LIVE", third)
        self.assertEqual(bot.active_mode, "live")
        self.assertTrue(bot._mode_armed)

    def test_live_mode_stays_locked_without_explicit_config(self):
        bot = TradingBot.__new__(TradingBot)
        bot.settings = SimpleNamespace(
            live_trading_enabled=False,
            live_trading_ack="",
            capital_configured=False,
            trading_mode="paper",
            enable_demo_orders=False,
        )
        bot.active_mode = "paper"
        response = bot._handle_mode_command("live", "42")
        self.assertIn("Live مقفل", response)
        self.assertEqual(bot.active_mode, "paper")

    def test_paper_mode_remains_available_without_capital_credentials(self):
        bot = TradingBot.__new__(TradingBot)
        bot.settings = SimpleNamespace(capital_configured=False)
        bot.active_mode = "live"
        bot._mode_armed = True
        bot._broker_clients = {}
        bot.capital = object()
        bot.market_stream = None
        bot.stream_cache = StreamCache()
        bot._last_multiframe_candidates = {}
        bot._market_cache = None
        bot._activate_mode("paper")
        self.assertEqual(bot.active_mode, "paper")
        self.assertFalse(bot._mode_armed)
        self.assertIsNone(bot.capital)


class BrokerContractValidationTests(unittest.TestCase):
    @staticmethod
    def contract(*, minimum_distance=0.1, maximum_distance=2.0):
        return Market(
            "TEST", "Test CFD", "SHARES", "TRADEABLE",
            decimal_places=2,
            min_deal_size=0.1,
            max_deal_size=100,
            lot_size=1,
            product="CFD",
            min_size_increment=0.3,
            min_stop_percent=minimum_distance,
            max_stop_percent=maximum_distance,
            min_stop_unit="POINTS",
            max_stop_unit="POINTS",
        )

    @staticmethod
    def settings():
        return SimpleNamespace(
            stop_distance_percent=0.5,
            take_profit_multiple=2,
            paper_risk_fraction=0.01,
            paper_max_leverage=100,
            max_epic_exposure_fraction=0.25,
        )

    def test_price_precision_size_increment_and_minimum_distance_are_applied(self):
        contract = self.contract(minimum_distance=1.0, maximum_distance=2.0)
        size, stop, target = calculate_broker_order(
            contract, "BUY", 100.0, 1_000, self.settings()
        )
        self.assertEqual(stop, 99.0)
        self.assertEqual(target, 102.0)
        self.assertEqual(size, 2.4)

    def test_sell_levels_round_conservatively_to_contract_precision(self):
        size, stop, target = calculate_broker_order(
            self.contract(), "SELL", 100.0, 1_000, self.settings()
        )
        self.assertEqual(stop, 100.5)
        self.assertEqual(target, 99.0)
        self.assertEqual(size, 2.4)

    def test_order_is_blocked_when_target_exceeds_broker_maximum(self):
        with self.assertRaisesRegex(RuntimeError, "broker maximum"):
            calculate_broker_order(
                self.contract(minimum_distance=0.1, maximum_distance=0.5),
                "BUY", 100.0, 1_000, self.settings(),
            )

    def test_order_is_blocked_when_stop_units_are_missing_or_unknown(self):
        contract = self.contract()
        contract = Market(**{**contract.__dict__, "min_stop_unit": "UNKNOWN"})
        with self.assertRaisesRegex(RuntimeError, "unsupported broker stop-distance unit"):
            calculate_broker_order(contract, "BUY", 100.0, 1_000, self.settings())


class BrokerOrderSafetyTests(unittest.TestCase):
    CONTRACT = {
        "epic": "TEST",
        "instrumentName": "Test CFD",
        "instrumentType": "SHARES",
        "marketStatus": "TRADEABLE",
        "currencyCode": "USD",
        "instrument": {"lotSize": 1, "currencyCode": "USD", "product": "CFD"},
        "snapshot": {"decimalPlacesFactor": 2},
        "dealingRules": {
            "minDealSize": {"value": 0.1},
            "maxDealSize": {"value": 100},
            "minSizeIncrement": {"value": 0.1},
            "minStopOrProfitDistance": {"value": 0.01, "unit": "POINTS"},
            "maxStopOrProfitDistance": {"value": 5.0, "unit": "POINTS"},
        },
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "journal.sqlite3")
        self.journal = Journal(self.path)
        self.cache = StreamCache()
        self.market = Market(
            "TEST", "Test CFD", "SHARES", "TRADEABLE", currency_code="USD", product="CFD"
        )
        self.signal = SimpleNamespace(epic="TEST", direction="BUY", market_name="Test CFD")

        class SettingsStub:
            max_daily_demo_orders = 5
            max_open_positions = 2
            stop_distance_percent = 0.5
            take_profit_multiple = 2
            paper_risk_fraction = 0.01
            paper_max_leverage = 100
            max_epic_exposure_fraction = 1.0

        self.settings = SettingsStub()
        self.bot = TradingBot.__new__(TradingBot)
        self.bot.settings = self.settings
        self.bot.journal = self.journal
        self.bot.stream_cache = self.cache
        self.bot.active_mode = "demo"
        self.bot._notify = Mock()

    def tearDown(self):
        self.tmp.cleanup()

    def set_quote(self, bid=100.0, offer=100.05, *, timestamp=None):
        stamp = timestamp if timestamp is not None else datetime.now(UTC).timestamp() * 1000
        self.cache.consume({
            "destination": "quote",
            "payload": {
                "epic": "TEST", "bid": bid, "ofr": offer, "product": "CFD",
                "timestamp": stamp,
            },
        })

    def make_capital(self, outcome="timeout", *, change_quote=False):
        cache = self.cache

        class FakeCapital:
            calls = 0
            posts = 0

            def market_details(self, epic):
                return BrokerOrderSafetyTests.CONTRACT

            def positions(self):
                return []

            def account_balance(self):
                return 1_000.0, "USD"

            def create_position(self, **kwargs):
                self.calls += 1
                if change_quote:
                    cache.consume({
                        "destination": "quote",
                        "payload": {
                            "epic": "TEST", "bid": 100.01, "ofr": 100.06,
                            "product": "CFD",
                            "timestamp": datetime.now(UTC).timestamp() * 1000,
                        },
                    })
                kwargs["before_submit"]()
                self.posts += 1
                if outcome == "timeout":
                    raise TimeoutError("simulated ambiguous POST result")
                return {"dealStatus": "ACCEPTED"}

        return FakeCapital()

    def test_ambiguous_post_persists_epic_lock_and_attempt_count_across_restart(self):
        self.set_quote()
        capital = self.make_capital("timeout")
        self.bot.capital = capital
        self.bot._submit_broker_signal(self.signal, self.market)
        self.assertEqual(self.journal.broker_attempt_status("TEST"), "AMBIGUOUS")
        reopened = Journal(self.path)
        self.bot.journal = reopened
        self.bot._submit_broker_signal(self.signal, self.market)
        self.assertEqual(capital.calls, 1)
        self.assertEqual(reopened.daily_broker_order_count(), 1)

    def test_successful_confirmation_is_recorded_separately_from_the_attempt(self):
        self.set_quote()
        self.bot.capital = self.make_capital("accepted")
        self.bot._submit_broker_signal(self.signal, self.market)
        self.assertEqual(self.journal.broker_attempt_status("TEST"), "CONFIRMED")
        self.assertFalse(self.journal.broker_epic_locked("TEST"))
        self.assertEqual(self.journal.daily_broker_order_count(), 1)
        self.assertEqual(self.journal.confirmed_broker_order_count(), 1)

    def test_quote_change_after_sizing_blocks_post_without_reserving_attempt(self):
        self.set_quote()
        capital = self.make_capital("accepted", change_quote=True)
        self.bot.capital = capital
        self.bot._submit_broker_signal(self.signal, self.market)
        self.assertEqual(capital.calls, 1)
        self.assertEqual(capital.posts, 0)
        self.assertEqual(self.journal.daily_broker_order_count(), 0)
        self.assertEqual(self.journal.broker_attempt_status("TEST"), "")

    def test_missing_or_stale_source_timestamp_blocks_before_order_call(self):
        self.set_quote(timestamp=(datetime.now(UTC).timestamp() - 121) * 1000)
        capital = self.make_capital("accepted")
        self.bot.capital = capital
        self.bot._submit_broker_signal(self.signal, self.market)
        self.assertEqual(capital.calls, 0)
        self.assertEqual(self.journal.daily_broker_order_count(), 0)


class StreamingTests(unittest.TestCase):
    def test_subscriptions_are_rotated_in_groups_no_larger_than_40(self):
        groups = chunks([f"E{i}" for i in range(85)])
        self.assertEqual([len(group) for group in groups], [40, 40, 5])
        message = subscription_message("OHLCMarketData.subscribe", "cst", "security", {"epics": groups[0]})
        import json
        payload = json.loads(message)
        self.assertEqual(len(payload["payload"]["epics"]), 40)

    def test_stream_cache_replaces_forming_bar_and_requires_all_frames(self):
        cache = StreamCache()
        for resolution in SUPPORTED_RESOLUTIONS:
            for index in range(20):
                cache.consume({
                    "destination": "ohlc.event",
                    "payload": {"epic": "AAPL", "resolution": resolution, "t": index, "c": 100 + index},
                })
        self.assertEqual(cache.aligned_trend("AAPL"), "BUY")
        cache.consume({
            "destination": "ohlc.event",
            "payload": {"epic": "AAPL", "resolution": "MINUTE", "t": 19, "c": 110},
        })
        _, bars = cache.snapshot()
        self.assertEqual(len(bars[("AAPL", "MINUTE")]), 20)

    def test_quote_cache_tracks_freshness_for_order_safety(self):
        cache = StreamCache()
        cache.consume({
            "destination": "quote",
            "payload": {
                "epic": "AAPL", "bid": 100, "product": "CFD",
                "timestamp": datetime.now(UTC).timestamp() * 1000,
            },
        })
        updated = cache.quote_updated_at["AAPL"]
        self.assertEqual(cache.quote_age("AAPL", now=updated + 20), 20)
        quote, age = cache.quote_snapshot("AAPL", now=updated + 20)
        self.assertEqual(quote["bid"], 100)
        self.assertEqual(age, 20)
        self.assertIsNone(cache.quote_age("MSFT"))

    def test_websocket_session_subscribes_to_quotes_and_all_frames_without_network(self):
        import json

        messages = []
        sent = []
        stream = None

        class FakeClient:
            def stream_credentials(self):
                return "cst", "security", "wss://example.invalid/connect"

        class FakeSocket:
            def send(self, value):
                sent.append(json.loads(value))

            def recv(self):
                if len(messages) == 0:
                    return json.dumps({
                        "destination": "quote",
                        "payload": {
                            "epic": "AAPL", "product": "CFD",
                            "timestamp": datetime.now(UTC).timestamp() * 1000,
                        },
                    })
                stream._stop.set()
                return ""

            def close(self):
                pass

        stream = CapitalWebSocketStream(
            FakeClient(), messages.append, socket_factory=lambda *args, **kwargs: FakeSocket()
        )
        stream._stream_batch(["AAPL"], rotate=False)
        self.assertEqual([item["destination"] for item in sent], ["marketData.subscribe", "OHLCMarketData.subscribe"])
        self.assertEqual(sent[1]["payload"]["resolutions"], list(SUPPORTED_RESOLUTIONS))
        self.assertEqual(messages[0]["payload"]["epic"], "AAPL")


class MultiTimeframeBacktestTests(unittest.TestCase):
    @staticmethod
    def fixture_prices():
        prices = [100.0] * 24 + [100.0 - i * 0.6 for i in range(12)] + [93.4 + i * 0.7 for i in range(40)]
        rows = []
        for index, close in enumerate(prices):
            prior = prices[index - 1] if index else close
            rows.append({
                "snapshotTimeUTC": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
                "openPrice": {"bid": prior - 0.01, "ask": prior + 0.01},
                "closePrice": {"bid": close - 0.01, "ask": close + 0.01},
                "highPrice": {"bid": max(prior, close) + 0.2, "ask": max(prior, close) + 0.22},
                "lowPrice": {"bid": min(prior, close) - 0.2, "ask": min(prior, close) - 0.18},
            })
        return rows

    def test_backtest_computes_real_metrics_from_local_fixture(self):
        result = backtest_timeframe("TEST", "MINUTE", self.fixture_prices())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.candles, len(self.fixture_prices()))
        self.assertGreaterEqual(result.trades, 1)
        self.assertGreaterEqual(result.max_drawdown_percent, 0)
        self.assertIn("ليس اختبارًا لنموذج FinBERT", report_text("TEST", [result], SimpleNamespace(
            paper_starting_balance=10_000,
            paper_account_currency="USD",
            paper_risk_fraction=0.01,
            max_epic_exposure_fraction=0.25,
            stop_distance_percent=0.5,
            take_profit_multiple=2,
        )))

    def test_backtest_applies_configured_per_epic_exposure_cap(self):
        rows = self.fixture_prices()
        capped = backtest_timeframe(
            "TEST", "MINUTE", rows, starting_balance=1_000,
            max_epic_exposure_fraction=0.25,
        )
        less_restricted = backtest_timeframe(
            "TEST", "MINUTE", rows, starting_balance=1_000,
            max_epic_exposure_fraction=1.0,
        )

        self.assertLessEqual(abs(capped.net_profit), abs(less_restricted.net_profit))
        self.assertGreaterEqual(capped.max_drawdown_percent, 0)

    def test_backtest_requests_each_supported_resolution_and_never_fills_missing_results(self):
        calls = []
        def fetch(epic, resolution, maximum):
            calls.append((epic, resolution, maximum))
            return self.fixture_prices() if resolution == "MINUTE" else []

        settings = SimpleNamespace(
            paper_starting_balance=10_000,
            paper_account_currency="USD",
            paper_risk_fraction=0.01,
            paper_max_leverage=100,
            max_epic_exposure_fraction=0.25,
            stop_distance_percent=0.5,
            take_profit_multiple=2,
        )
        results = run_all_timeframes("TEST", fetch, settings)
        self.assertEqual([call[1] for call in calls], list(SUPPORTED_RESOLUTIONS))
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(results[1].status, "no_data")
        self.assertIn("لا توجد بيانات", results[1].as_text("USD"))

    def test_backtest_reports_the_actual_22_candle_minimum(self):
        result = backtest_timeframe("TEST", "MINUTE", self.fixture_prices()[:21])
        self.assertEqual(result.status, "insufficient")
        self.assertIn("21 شمعة؛ يلزم 22 على الأقل", result.as_text("USD"))

    def test_capital_history_paginates_with_fixtures_and_reports_completion(self):
        client = CapitalClient.__new__(CapitalClient)
        def row(index):
            return {"snapshotTimeUTC": f"2026-01-01T00:0{index}:00", "closePrice": {"bid": 1, "ask": 1.1}}
        pages = [
            [row(3), row(4), row(5)],
            [row(0), row(1), row(2)],
            [],
        ]
        def fetch(epic, resolution, maximum, *, from_time="", to_time=""):
            return pages.pop(0)
        with patch.object(client, "historical_prices", side_effect=fetch) as mocked:
            rows, complete = client.historical_prices_all("TEST", "MINUTE", maximum=3, page_limit=5)
        self.assertTrue(complete)
        self.assertEqual(len(rows), 6)
        self.assertEqual(mocked.call_count, 3)

    def test_history_page_cap_is_reported_as_incomplete(self):
        client = CapitalClient.__new__(CapitalClient)
        rows = [
            {"snapshotTimeUTC": f"2026-01-01T00:0{i}:00", "closePrice": {"bid": 1, "ask": 1.1}}
            for i in range(3)
        ]
        with patch.object(client, "historical_prices", return_value=rows):
            result, complete = client.historical_prices_all("TEST", "MINUTE", maximum=3, page_limit=1)
        self.assertEqual(len(result), 3)
        self.assertFalse(complete)

    def test_short_first_history_page_is_complete_without_extra_request(self):
        client = CapitalClient.__new__(CapitalClient)
        rows = [{"snapshotTimeUTC": "2026-01-01T00:00:00", "closePrice": {"bid": 1, "ask": 1.1}}]
        with patch.object(client, "historical_prices", return_value=rows) as mocked:
            result, complete = client.historical_prices_all("TEST", "MINUTE", maximum=3)
        self.assertTrue(complete)
        self.assertEqual(result, rows)
        mocked.assert_called_once()

    def test_broker_order_method_fails_closed_when_gate_is_off(self):
        client = CapitalClient.__new__(CapitalClient)
        client.settings = SimpleNamespace(enable_demo_orders=False)
        client.environment = "demo"
        with self.assertRaisesRegex(RuntimeError, "locked"):
            client.create_position(
                epic="TEST", direction="BUY", size=1,
                stop_level=99, profit_level=102, product="CFD",
            )

    def test_broker_order_requires_cfd_and_confirms_without_network(self):
        client = CapitalClient.__new__(CapitalClient)
        client.settings = SimpleNamespace(enable_demo_orders=True)
        client.environment = "demo"
        client._request = Mock(side_effect=[{"dealReference": "ref-1"}, {"dealStatus": "ACCEPTED"}])
        confirmation = client.create_position(
            epic="TEST", direction="BUY", size=1,
            stop_level=99, profit_level=102, product="CFD",
        )
        self.assertEqual(confirmation["dealStatus"], "ACCEPTED")
        self.assertEqual([call.args[0] for call in client._request.call_args_list], ["POST", "GET"])
        client._request.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "CFD"):
            client.create_position(
                epic="TEST", direction="BUY", size=1,
                stop_level=99, profit_level=102, product="SHARES",
            )
        client._request.assert_not_called()


class BacktestTests(unittest.TestCase):
    def test_backtest_compounds_on_the_updated_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.csv"
            path.write_text(
                "timestamp,epic,direction,entry_price,exit_price\n"
                "2026-01-01T00:00:00Z,EURUSD,BUY,1.00,1.10\n"
                "2026-01-02T00:00:00Z,EURUSD,SELL,1.00,1.05\n",
                encoding="utf-8",
            )
            report = run_backtest(
                path,
                starting_balance=10_000,
                max_leverage=2,
                risk_fraction=0.01,
                stop_distance_percent=0.5,
            )
        self.assertEqual(report.trades, 2)
        self.assertEqual(report.wins, 1)
        self.assertEqual(report.losses, 1)
        self.assertAlmostEqual(report.final_balance, 10_800)
        self.assertAlmostEqual(report.net_return_percent, 8.0)

    def test_backtest_default_is_unlevered(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.csv"
            path.write_text(
                "timestamp,epic,direction,entry_price,exit_price\n"
                "2026-01-01T00:00:00Z,EURUSD,BUY,1.00,1.10\n"
                "2026-01-02T00:00:00Z,EURUSD,SELL,1.00,1.05\n",
                encoding="utf-8",
            )
            report = run_backtest(path)
        self.assertAlmostEqual(report.net_return_percent, 4.5)


if __name__ == "__main__":
    unittest.main()
