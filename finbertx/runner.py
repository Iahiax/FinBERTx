from __future__ import annotations

import logging
import secrets
import threading
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from finbertx.capital import CapitalClient, CapitalDemoClient
from finbertx.finbert import FinBertEngine
from finbertx.journal import Journal
from finbertx.network import ApiError
from finbertx.multiframe_backtest import report_text, run_all_timeframes
from finbertx.paper import PaperTradingEngine
from finbertx.settings import Settings
from finbertx.sources import EconomicCalendar, FinnhubSource
from finbertx.strategy import currency_pair, is_in_high_impact_blackout, make_signal
from finbertx.streaming import CapitalWebSocketStream, StreamCache
from finbertx.telegram import TelegramBot
from finbertx.types import EconomicEvent, Market, NewsItem, SentimentScores, TradeSignal

logger = logging.getLogger("finbertx")


def _quantize_price(value: float, decimals: int, rounding: str) -> float:
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quantum, rounding=rounding))


def _distance_in_price(value: float, unit: str, entry: float) -> float:
    if unit == "PERCENTAGE":
        return entry * value / 100.0
    if unit == "POINTS":
        # Capital.com returns POINTS as a price-distance value; decimalPlacesFactor
        # is used separately to round the submitted price levels.
        return value
    raise RuntimeError(f"unsupported broker stop-distance unit: {unit or 'missing'}")


def calculate_broker_order(
    contract: Market,
    direction: str,
    entry_price: float,
    balance: float,
    settings: Settings,
) -> tuple[float, float, float]:
    """Return size/stop/target only when all broker contract constraints are known."""
    if direction not in {"BUY", "SELL"} or entry_price <= 0 or balance <= 0:
        raise RuntimeError("invalid direction, entry price, or account balance")
    if not 0 <= contract.decimal_places <= 10:
        raise RuntimeError("market price precision is unavailable")
    if (
        contract.lot_size <= 0
        or contract.min_deal_size <= 0
        or contract.max_deal_size < contract.min_deal_size
        or contract.min_size_increment <= 0
    ):
        raise RuntimeError("market size limits or minSizeIncrement are unavailable")
    minimum_distance = _distance_in_price(
        contract.min_stop_percent, contract.min_stop_unit, entry_price,
    )
    maximum_distance = _distance_in_price(
        contract.max_stop_percent, contract.max_stop_unit, entry_price,
    )
    if minimum_distance < 0 or maximum_distance <= 0 or minimum_distance > maximum_distance:
        raise RuntimeError("market stop-distance bounds are invalid")

    requested_stop = entry_price * settings.stop_distance_percent / 100.0
    stop_distance = max(requested_stop, minimum_distance)
    if stop_distance > maximum_distance:
        raise RuntimeError("configured stop distance exceeds the broker maximum")
    target_distance = max(
        stop_distance * settings.take_profit_multiple, minimum_distance
    )
    if target_distance > maximum_distance:
        raise RuntimeError("configured profit distance exceeds the broker maximum")

    if direction == "BUY":
        stop_level = _quantize_price(
            entry_price - stop_distance, contract.decimal_places, ROUND_FLOOR
        )
        profit_level = _quantize_price(
            entry_price + target_distance, contract.decimal_places, ROUND_CEILING
        )
    else:
        stop_level = _quantize_price(
            entry_price + stop_distance, contract.decimal_places, ROUND_CEILING
        )
        profit_level = _quantize_price(
            entry_price - target_distance, contract.decimal_places, ROUND_FLOOR
        )
    actual_stop = abs(entry_price - stop_level)
    actual_profit = abs(profit_level - entry_price)
    if stop_level <= 0 or profit_level <= 0:
        raise RuntimeError("rounded stop/profit levels must remain positive")
    if not (
        minimum_distance <= actual_stop <= maximum_distance
        and minimum_distance <= actual_profit <= maximum_distance
    ):
        raise RuntimeError("rounded stop/profit levels violate broker distance bounds")

    risk_size = (
        balance * settings.paper_risk_fraction
        / (contract.lot_size * actual_stop)
    )
    leverage_size = (
        balance * settings.paper_max_leverage
        / (entry_price * contract.lot_size)
    )
    epic_exposure_size = (
        balance * settings.max_epic_exposure_fraction
        / (entry_price * contract.lot_size)
    )
    raw_size = min(
        risk_size, leverage_size, epic_exposure_size, contract.max_deal_size
    )
    increment = Decimal(str(contract.min_size_increment))
    size = (Decimal(str(raw_size)) / increment).to_integral_value(
        rounding=ROUND_FLOOR
    ) * increment
    if size < Decimal(str(contract.min_deal_size)):
        raise RuntimeError("risk-sized deal is below the broker minimum size")
    if size > Decimal(str(contract.max_deal_size)):
        raise RuntimeError("deal size exceeds the broker maximum")
    return float(size), stop_level, profit_level


class TradingBot:
    """News-analysis bot with gated paper, demo, and explicitly confirmed live modes."""

    def __init__(self, settings: Settings):
        settings.validate_for_run()
        self.settings = settings
        self.news = FinnhubSource(settings.finnhub_api_key)
        self.calendar = EconomicCalendar(
            finnhub_key=settings.finnhub_api_key,
            fmp_key=settings.fmp_api_key,
            fred_key=settings.fred_api_key,
        )
        self.capital = (
            CapitalDemoClient(settings) if settings.capital_configured else None
        )
        self.active_mode = settings.trading_mode
        self._mode_armed = False
        self._pending_live_confirmations: dict[str, tuple[str, float]] = {}
        self._broker_clients: dict[str, CapitalClient] = {}
        if self.capital:
            self._broker_clients["demo"] = self.capital
        self.stream_cache = StreamCache()
        self.market_stream: CapitalWebSocketStream | None = None
        self._last_multiframe_candidates: dict[str, str] = {}
        self._multiframe_backtests: set[str] = set()
        self._multiframe_backtest_lock = threading.Lock()
        self.telegram = (
            TelegramBot(settings.telegram_bot_token, settings.telegram_chat_id)
            if settings.telegram_configured
            else None
        )
        self.engine = FinBertEngine()
        self.journal = Journal(settings.journal_path)
        self.paper = PaperTradingEngine(
            self.journal,
            starting_balance=settings.paper_starting_balance,
            account_currency=settings.paper_account_currency,
            max_leverage=settings.paper_max_leverage,
            risk_fraction=settings.paper_risk_fraction,
            stop_distance_percent=settings.stop_distance_percent,
            take_profit_multiple=settings.take_profit_multiple,
            max_open_positions=settings.max_open_positions,
            max_daily_trades=settings.max_daily_demo_orders,
            daily_loss_limit_fraction=settings.paper_daily_loss_limit_fraction,
            max_epic_exposure_fraction=settings.max_epic_exposure_fraction,
        )
        self._market_cache: tuple[float, list[Market]] | None = None
        self._last_any_market_open: bool | None = None

    def run_forever(self) -> None:
        self._notify(
            "بدأ FinBERTx على CPU. الوضع الحالي: "
            f"{self.active_mode.upper()}; لا يبدأ توجيه الأوامر إلا بعد اختيار الوضع صراحةً عبر Telegram."
        )
        while True:
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception as exc:
                # Do not log request URLs or input text, which could contain credentials/PII.
                logger.exception("Polling cycle failed (%s).", type(exc).__name__)
                self._notify(
                    "تعذر إكمال دورة التحليل. إن كان وضع الوسيط مفعّلًا ونتيجة أمر ملتبسة، "
                    "افحص الحساب قبل أي إعادة محاولة."
                )
            elapsed = time.monotonic() - started
            time.sleep(max(1, self.settings.poll_interval_seconds - elapsed))

    def poll_once(self) -> None:
        self._handle_telegram_updates()
        events: list[EconomicEvent] = []
        if self.calendar.providers:
            try:
                events = self.calendar.upcoming()
            except ApiError:
                logger.warning("Economic calendar unavailable; this cycle is analysis-only.")

        items = self.news.market_news(self.settings.max_news_per_cycle)
        markets = self._markets() if self.capital else []
        market_map = {market.epic: market for market in markets}
        self._notify_market_state(markets)
        for closed in self.paper.update_positions(market_map):
            self.journal.add_event(
                "paper_close",
                f"{closed.direction} {closed.reason}; pnl={closed.pnl:.2f}; "
                f"balance={closed.balance:.2f}",
                epic=closed.epic,
            )
            self._notify(
                f"إغلاق محاكاة {closed.direction} {closed.epic} ({closed.reason})؛ "
                f"الربح/الخسارة {closed.pnl:+.2f} {self.settings.paper_account_currency}؛ "
                f"الرصيد المتراكم {closed.balance:.2f}."
            )

        for item in items:
            if self.journal.get_value(f"seen:{item.news_id}") == "true":
                continue
            scores = self.engine.analyze(f"{item.headline}\n{item.summary}".strip())
            self.journal.mark_news_seen(item.news_id)
            self.journal.set_value(f"seen:{item.news_id}", "true")
            self.journal.add_event(
                "analysis",
                f"{scores.label}:{scores.positive:.4f}/{scores.negative:.4f}/{scores.neutral:.4f}",
            )
            if self.settings.telegram_verbose_steps:
                self._notify(self._analysis_message(item, scores))
            self._process_signals(item, scores, markets, events)

    def _markets(self) -> list[Market]:
        if not self.capital:
            return []
        now = time.monotonic()
        if self._market_cache and now - self._market_cache[0] < 30:
            return self._market_cache[1]
        markets = self.capital.all_markets()
        self._market_cache = (now, markets)
        self._ensure_market_stream(markets)
        return markets

    def _ensure_market_stream(self, markets: list[Market]) -> None:
        eligible = [
            market.epic for market in markets
            if market.epic and market.streaming_prices_available
        ]
        if not eligible or not self.capital:
            return
        if self.market_stream is None:
            self.market_stream = CapitalWebSocketStream(
                self.capital,
                self._on_stream_message,
            )
            self.market_stream.start(eligible)
        else:
            self.market_stream.update_epics(eligible)

    def _on_stream_message(self, message: dict) -> None:
        self.stream_cache.consume(message)
        if str(message.get("destination") or "") != "ohlc.event":
            return
        payload = message.get("payload") or {}
        epic = str(payload.get("epic") or "")
        direction = self.stream_cache.aligned_trend(epic)
        previous = self._last_multiframe_candidates.get(epic, "")
        if direction and direction != previous:
            self._last_multiframe_candidates[epic] = direction
            self.journal.add_event(
                "multiframe_candidate",
                f"{direction} aligned across all supported resolutions; observation only",
                epic=epic,
            )
            self._notify(
                f"رصد توافق سعري {direction} على {epic} عبر الأطر الثمانية. "
                "هذه ملاحظة فنية وليست إشارة FinBERT ولا تُرسل أمر تداول."
            )
        elif not direction and previous:
            self._last_multiframe_candidates.pop(epic, None)

    def _notify_market_state(self, markets: list[Market]) -> None:
        fx = [market for market in markets if currency_pair(market)]
        if not fx:
            return
        is_open = any(market.market_status == "TRADEABLE" for market in fx)
        if self._last_any_market_open is None:
            self._last_any_market_open = is_open
            return
        if is_open != self._last_any_market_open:
            self._last_any_market_open = is_open
            message = (
                "رُصدت عودة سوق الفوركس للتداول؛ استؤنفت مراقبة الإشارات."
                if is_open
                else "سوق الفوركس مغلق/غير قابل للتداول؛ أوقفت المحاكاة فتح مراكز جديدة."
            )
            self._notify(message)

    def _process_signals(
        self,
        item: NewsItem,
        scores: SentimentScores,
        markets: list[Market],
        events: list[EconomicEvent],
    ) -> None:
        for market in markets:
            signal = make_signal(
                item,
                scores,
                market,
                min_confidence=self.settings.min_sentiment_confidence,
            )
            if signal is None:
                continue
            self.journal.add_event(
                "signal",
                f"{signal.direction} {signal.market_name} | {item.headline[:180]}",
                epic=signal.epic,
            )
            if market.market_status != "TRADEABLE":
                self.journal.add_event("market_closed", "signal skipped; market not tradeable", market.epic)
                continue
            pair = currency_pair(market)
            if pair:
                blackout = is_in_high_impact_blackout(
                    events,
                    pair,
                    before_minutes=self.settings.high_impact_blackout_before_minutes,
                    after_minutes=self.settings.high_impact_blackout_after_minutes,
                )
                if blackout:
                    self._notify(
                        f"تجاوز إشارة {market.name}: حدث اقتصادي عالي التأثير "
                        f"({blackout.name}) ضمن فترة الحظر."
                    )
                    continue
            if self.journal.paused:
                self._notify(f"إشارة {signal.direction} على {market.name} معلّقة: البوت متوقف.")
                continue
            if self._broker_orders_enabled():
                self._submit_broker_signal(signal, market)
                continue
            result = self.paper.open_signal(
                signal,
                market,
                signal_id=f"{item.news_id}:{market.epic}",
                markets={m.epic: m for m in markets},
            )
            if result.fill:
                fill = result.fill
                self._notify(
                    f"محاكاة ورقية فقط: {fill.direction} {signal.market_name} "
                    f"بسعر {fill.entry_price:g}؛ القيمة الاسمية {fill.notional:.2f} "
                    f"{fill.account_currency}، الرافعة المستخدمة {fill.leverage:.2f}:1 "
                    f"(السقف {self.settings.paper_max_leverage:.0f}:1)، "
                    f"وقف {fill.stop_price:g} وهدف {fill.target_price:g}. لا أمر للوسيط."
                )
            else:
                self._notify(
                    self._signal_message(signal, simulated=True)
                    + f"\nلم يُفتح مركز محلي: {result.reason}."
                )

    def _broker_orders_enabled(self) -> bool:
        if self.active_mode == "demo":
            return bool(self.settings.enable_demo_orders and self._mode_armed)
        if self.active_mode == "live":
            return bool(
                self.settings.live_trading_enabled
                and self.settings.live_trading_ack
                and self.telegram
                and self._mode_armed
            )
        return False

    def _submit_broker_signal(self, signal: TradeSignal, market: Market) -> None:
        attempt_reserved = False
        attempt_confirmed = False
        try:
            if not self.capital:
                raise RuntimeError("Capital.com credentials are not configured.")
            if self.journal.broker_epic_locked(signal.epic):
                existing_attempt = self.journal.broker_attempt_status(signal.epic)
                raise RuntimeError(
                    f"broker EPIC is locked after an earlier {existing_attempt.lower()} attempt"
                )
            if self.journal.daily_broker_order_count() >= self.settings.max_daily_demo_orders:
                raise RuntimeError("daily broker-order limit reached")
            details = self.capital.market_details(signal.epic)
            if isinstance(details, dict) and isinstance(details.get("market"), dict):
                details = details["market"]
            contract = Market.from_payload(details if isinstance(details, dict) else {})
            if contract.market_status != "TRADEABLE":
                raise RuntimeError("market is not tradeable")
            quote, quote_age = self.stream_cache.quote_snapshot(signal.epic)
            quote = quote or {}
            if quote_age is None or quote_age > 120:
                raise RuntimeError("the latest streaming quote is missing or older than 120 seconds")
            product = str(quote.get("product") or "").upper()
            if product != "CFD":
                raise RuntimeError("the live quote did not confirm this instrument is a CFD")
            current_positions = self.capital.positions()
            if len(current_positions) >= self.settings.max_open_positions:
                raise RuntimeError("maximum open positions reached")
            if any(
                str((row.get("position") or {}).get("epic") or "") == signal.epic
                for row in current_positions
            ):
                raise RuntimeError("a position is already open on this market")
            balance, account_currency = self.capital.account_balance()
            pair = currency_pair(market)
            instrument_currency = contract.currency_code or (pair[1] if pair else "")
            if not instrument_currency or instrument_currency != account_currency:
                raise RuntimeError(
                    "account/instrument currency conversion is not supported; order blocked"
                )
            size = stop_level = profit_level = 0.0
            used_bid = used_offer = 0.0

            def read_current_quote() -> tuple[dict, float, float]:
                row, age = self.stream_cache.quote_snapshot(signal.epic)
                row = row or {}
                if age is None or age > 120:
                    raise RuntimeError(
                        "the latest streaming quote is missing or older than 120 seconds"
                    )
                if str(row.get("product") or "").upper() != "CFD":
                    raise RuntimeError("the latest quote is not confirmed as a CFD")
                try:
                    current_bid = float(row.get("bid"))
                    current_offer = float(row.get("ofr", row.get("offer")))
                except (TypeError, ValueError):
                    raise RuntimeError("a current live bid/offer quote is unavailable") from None
                if current_bid <= 0 or current_offer <= 0 or current_bid > current_offer:
                    raise RuntimeError("a current live bid/offer quote is invalid")
                return row, current_bid, current_offer

            _, used_bid, used_offer = read_current_quote()
            used_entry = used_offer if signal.direction == "BUY" else used_bid
            size, stop_level, profit_level = calculate_broker_order(
                contract, signal.direction, used_entry, balance, self.settings
            )

            def before_submit() -> None:
                nonlocal attempt_reserved
                _, current_bid, current_offer = read_current_quote()
                decimals = contract.decimal_places
                if (
                    _quantize_price(current_bid, decimals, ROUND_HALF_UP)
                    != _quantize_price(used_bid, decimals, ROUND_HALF_UP)
                    or _quantize_price(current_offer, decimals, ROUND_HALF_UP)
                    != _quantize_price(used_offer, decimals, ROUND_HALF_UP)
                ):
                    raise RuntimeError("the live quote changed after risk sizing; order blocked")
                reservation = self.journal.reserve_broker_attempt(
                    signal.epic, self.active_mode,
                    self.settings.max_daily_demo_orders,
                )
                if reservation == "daily_limit":
                    raise RuntimeError("daily broker-order limit reached")
                if reservation == "locked":
                    raise RuntimeError("broker EPIC is already locked by an earlier attempt")
                attempt_reserved = True

            result = self.capital.create_position(
                epic=signal.epic,
                direction=signal.direction,
                size=size,
                stop_level=stop_level,
                profit_level=profit_level,
                product=product,
                before_submit=before_submit,
            )
            deal_status = str(result.get("dealStatus") or result.get("status") or "unknown")
            if deal_status.upper() not in {"ACCEPTED", "OPEN"}:
                raise RuntimeError(
                    "broker confirmation was not accepted; inspect the account before retrying"
                )
            self.journal.mark_broker_attempt_confirmed(signal.epic, deal_status)
            attempt_confirmed = True
            self.journal.add_event(
                "broker_order",
                f"{self.active_mode} {signal.direction} size={size:g}; status={deal_status}",
                epic=signal.epic,
            )
            self._notify(
                f"تأكيد الوسيط: {self.active_mode.upper()} {signal.direction} {signal.epic}، "
                f"الحجم {size:g}، الحالة {deal_status}."
            )
        except Exception as exc:
            if attempt_reserved and not attempt_confirmed:
                self.journal.mark_broker_attempt_ambiguous(
                    signal.epic, type(exc).__name__
                )
            # Never retry an ambiguous POST; its durable EPIC lock survives restart.
            logger.warning("Broker order was blocked or unconfirmed (%s).", type(exc).__name__)
            if attempt_confirmed:
                message = (
                    f"أكد الوسيط أمر {signal.direction} على {signal.epic}، لكن تعذر إكمال تسجيله محليًا. "
                    "تحقق من الحساب؛ لن أعيد الإرسال."
                )
            elif attempt_reserved:
                message = (
                    f"نتيجة إرسال {signal.direction} على {signal.epic} ملتبسة ({exc}). "
                    "المحاولة محفوظة والأداة مقفلة؛ افحص الحساب قبل أي إجراء يدوي."
                )
            else:
                message = (
                    f"حُجب أمر {signal.direction} على {signal.epic} قبل POST ({exc})؛ "
                    "لم تُسجل محاولة إرسال."
                )
            self._notify(message)

    def _handle_telegram_updates(self) -> None:
        if not self.telegram:
            return
        try:
            updates = self.telegram.updates()
        except ApiError:
            logger.warning("Telegram update poll failed.")
            return
        allowed = self.settings.telegram_allowed_chat_ids
        for update in updates:
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            if chat.get("type") != "private" or chat_id not in allowed:
                continue
            text = str(message.get("text") or "").strip()
            if not text:
                continue
            parts = text.split(maxsplit=1)
            command = parts[0].split("@", 1)[0].lower() if text.startswith("/") else ""
            argument = parts[1].strip() if len(parts) > 1 else ""
            if command == "/analyze":
                if argument:
                    self._reply_analysis(argument, chat_id)
                else:
                    self._notify("أرسل النص بعد الأمر، مثل: /analyze Central bank raises rates", chat_id)
            elif command in {
                "/help", "/start", "/status", "/pause", "/resume", "/backtest",
                "/signals", "/risk", "/mode",
            }:
                self._run_command(command, chat_id, argument)
            elif command:
                self._notify("أمر غير معروف. أرسل /help لعرض الأوامر.", chat_id)
            else:
                # Free text in a private, allowlisted chat is analyzed as financial text.
                self._reply_analysis(text[:4000], chat_id)

    def _reply_analysis(self, text: str, chat_id: str) -> None:
        try:
            scores = self.engine.analyze(text)
        except (RuntimeError, ValueError):
            self._notify("تعذر تحليل النص حاليًا. تحقق من تثبيت نموذج FinBERT.", chat_id)
            return
        self.journal.add_event(
            "telegram_analysis",
            f"{scores.label}:{scores.positive:.4f}/{scores.negative:.4f}/{scores.neutral:.4f}",
        )
        self._notify(self._sentiment_text(scores), chat_id)

    def _run_command(self, command: str, reply_to: str, argument: str = "") -> None:
        if command in {"/help", "/start"}:
            text = (
                "أنا محلل مشاعر مالية يعمل بـ FinBERT على CPU؛ التصنيف ليس توصية استثمارية.\n"
                "أرسل نصًا ماليًا مباشرة أو استخدم /analyze <text> لتحليل Positive/Negative/Neutral.\n"
                "/status — حالة التشغيل والرصيد الورقي\n"
                "/pause — إيقاف فتح مراكز جديدة\n"
                "/resume — استئناف فتح مراكز جديدة وفق الوضع الحالي\n"
                "/backtest <EPIC> — تقرير أسعار تاريخي نصي متعدد الأطر؛ أرسل /backtest لعرض الاستخدام\n"
                "/signals — ملخص الإشارات\n"
                "/risk — حدود المخاطرة\n/mode — عرض الوضع\n"
                "/mode paper|demo|live — تبديل الوضع؛ Live يتطلب بوابات إعداد وتأكيدًا ثانيًا\n"
                "/mode confirm_live <code> — تأكيد Live برمز مؤقت\n"
                "/help — المساعدة"
            )
        elif command == "/status":
            markets = self._markets() if self.capital else []
            market_map = {market.epic: market for market in markets}
            paper = self.paper.summary(market_map)
            fx_open = sum(
                1 for market in markets
                if currency_pair(market) and market.market_status == "TRADEABLE"
            )
            text = (
                f"الحالة: {'متوقف مؤقتًا' if self.journal.paused else 'يعمل'}\n"
                f"الوضع: {getattr(self, 'active_mode', self.settings.trading_mode)}\n"
                f"الجهاز: CPU\nأزواج الفوركس المتاحة الآن: {fx_open}\n"
                f"أدوات السوق المتاحة: {len(markets)} | أسعار بث حديثة: {len(self.stream_cache.quotes)}\n"
                f"الرصيد الورقي المحلي: {paper['balance']:.2f} {paper['account_currency']}\n"
                f"حقوق المحاكاة التقديرية: {paper['equity']:.2f}\n"
                f"مراكز المحاكاة: مفتوحة {paper['open_positions']} | مغلقة {paper['closed_positions']}\n"
                f"ربح/خسارة المحاكاة المحققة: {paper['realized_pnl']:+.2f}\n"
                f"سقف رافعة الإعداد: {paper['max_leverage']:.0f}:1\n"
                f"ملخص الأخبار/الإشارات: {self.journal.summary()}"
            )
        elif command == "/pause":
            self.journal.set_paused(True)
            text = "أوقفت فتح المراكز الجديدة (المحاكاة وأوامر الوسيط)؛ يستمر تحليل الأخبار ومتابعة المراكز القائمة."
        elif command == "/resume":
            self.journal.set_paused(False)
            text = "استؤنف فتح المراكز وفق الوضع الحالي؛ يظل Live خاضعًا لبواباته وتأكيده الصريح."
        elif command == "/signals":
            text = f"الملخص الحالي: {self.journal.summary()}"
        elif command == "/risk":
            daily = self.paper.daily_loss_status()
            text = (
                f"سقف الرافعة الورقية: {self.settings.paper_max_leverage:.0f}:1 كحد أقصى، وليس هدفًا.\n"
                f"المخاطرة المستهدفة: {self.settings.paper_risk_fraction:.2%} من حقوق الحساب الحالية؛ "
                f"التعرض الاسمي الأقصى لكل أداة: {self.settings.max_epic_exposure_fraction:.0%} من الرصيد.\n"
                f"حد خسارة Paper اليومي: {self.settings.paper_daily_loss_limit_fraction:.2%} "
                f"من حقوق بداية يوم UTC؛ P&L اليوم {daily['pnl']:+.2f} / حد الخسارة {daily['loss_limit']:.2f}.\n"
                f"وقف الخسارة الافتراضي: {self.settings.stop_distance_percent:.2f}%؛ "
                f"حد المراكز: {self.settings.max_open_positions}؛ سقف أوامر الوسيط اليومية: "
                f"{self.settings.max_daily_demo_orders}.\n"
                "حد الخسارة اليومي يحمي دفتر Paper فقط؛ لا يحتسب رصيد الوسيط أو مراكزه الخارجية، "
                "ولا يغيّر رافعة الوسيط."
            )
        elif command == "/mode":
            text = self._handle_mode_command(argument, reply_to)
            if text is None:
                return
        elif command == "/backtest":
            if argument.strip():
                text = self._multiframe_backtest_message(argument.strip(), reply_to)
            else:
                text = "الاستخدام: /backtest <EPIC> — أرسل رمز سوق واحدًا مثل EURUSD أو AAPL. سيصل التقرير نصيًا في هذه المحادثة."
        if text:
            self._notify(text, chat_id=reply_to)

    def _multiframe_backtest_message(self, epic: str, chat_id: str) -> str:
        if not self.capital:
            return (
                "تعذر تشغيل الباكتيست: بيانات Capital.com غير مهيأة. "
                "لم تُستخدم بيانات بديلة ولم تُعرض أرقام تقديرية."
            )
        if not epic or len(epic) > 40 or any(ch.isspace() for ch in epic):
            return "الاستخدام: /backtest <EPIC> — أرسل رمز سوق واحدًا مثل EURUSD أو AAPL."
        epic = epic.upper()
        with self._multiframe_backtest_lock:
            if epic in self._multiframe_backtests:
                return f"باكتيست {epic} جارٍ بالفعل؛ سأرسل تقريره عند اكتماله."
            self._multiframe_backtests.add(epic)
        client = self.capital
        threading.Thread(
            target=self._run_multiframe_backtest,
            args=(epic, client, chat_id),
            name=f"backtest-{epic}",
            daemon=True,
        ).start()
        return (
            f"بدأ جلب الأسعار التاريخية لـ{epic} على الأطر الثمانية عبر طلبات قراءة فقط. "
            "سيصل التقرير هنا عند اكتماله؛ الأرقام لن تُعرض إن لم تتوفر بيانات."
        )

    def _run_multiframe_backtest(self, epic: str, client: CapitalClient, chat_id: str) -> None:
        try:
            results = run_all_timeframes(
                epic,
                client.historical_prices_all,
                self.settings,
            )
            self._notify(report_text(epic, results, self.settings), chat_id)
        except Exception as exc:
            logger.warning("Historical backtest failed (%s).", type(exc).__name__)
            self._notify(
                f"تعذر إكمال باكتيست {epic} بسبب خطأ في جلب البيانات؛ لم تُحسب نتائج بديلة.",
                chat_id,
            )
        finally:
            with self._multiframe_backtest_lock:
                self._multiframe_backtests.discard(epic)

    def _handle_mode_command(self, argument: str, chat_id: str) -> str | None:
        parts = argument.split()
        mode = getattr(self, "active_mode", self.settings.trading_mode)
        if not parts:
            if mode == "live":
                return "الوضع الحالي: LIVE. قد تُرسل أوامر حقيقية وفق الإشارات؛ /mode paper يعيد المحاكاة المحلية."
            if mode == "demo":
                orders_enabled = bool(
                    self.settings.enable_demo_orders and getattr(self, "_mode_armed", False)
                )
                execution = (
                    "قد تُرسل أوامر Demo في هذه الجلسة"
                    if orders_enabled
                    else "قراءة/محاكاة فقط؛ أوامر Demo مقفلة حتى اختيار /mode demo صراحةً"
                )
                return f"الوضع الحالي: DEMO — {execution}."
            return "الوضع الحالي: PAPER — محاكاة محلية بلا أوامر وسيط. استخدم /mode demo أو /mode live لتغيير الوضع."

        action = parts[0].lower()
        if action in {"paper", "demo"}:
            try:
                self._activate_mode(action)
            except (ValueError, RuntimeError) as exc:
                return f"تعذر تغيير الوضع: {exc}"
            if action == "demo" and self.settings.enable_demo_orders:
                return "تم التحويل إلى DEMO؛ قد تُرسل أوامر إلى حساب Capital.com التجريبي عند ظهور إشارة مؤهلة."
            if action == "demo":
                return "تم التحويل إلى DEMO للمراقبة والمحاكاة المحلية؛ إرسال أوامر الوسيط ما زال مقفلاً (ENABLE_DEMO_ORDERS=false)."
            return "تم التحويل إلى PAPER؛ محاكاة محلية فقط، ولا تُرسل أوامر إلى الوسيط."

        if action == "live":
            if not self.settings.live_trading_enabled or self.settings.live_trading_ack != "I_UNDERSTAND_LIVE_CFD_RISK":
                return "Live مقفل. يلزم تفعيل LIVE_TRADING_ENABLED وضبط LIVE_TRADING_ACK الصحيح أولًا؛ لم يتغير الوضع."
            if not self.settings.capital_configured:
                return "Live مقفل: بيانات اعتماد Capital.com غير مكتملة؛ لم يتغير الوضع."
            nonce = secrets.token_hex(3).upper()
            pending = getattr(self, "_pending_live_confirmations", None)
            if pending is None:
                self._pending_live_confirmations = {}
                pending = self._pending_live_confirmations
            pending[chat_id] = (nonce, time.monotonic() + 180)
            return (
                "طلب التحويل إلى LIVE بدأ، ولم يتغير الوضع بعد. قد يرسل البوت أوامر حقيقية. "
                f"للتأكيد خلال 3 دقائق أرسل: /mode confirm_live {nonce} — أو /mode cancel للإلغاء."
            )

        if action == "confirm_live":
            pending = getattr(self, "_pending_live_confirmations", {})
            request = pending.get(chat_id)
            if not request or len(parts) != 2 or time.monotonic() > request[1] or parts[1].upper() != request[0]:
                pending.pop(chat_id, None)
                return "لا يوجد تأكيد Live صالح لهذه المحادثة، أو انتهت صلاحيته؛ لا تغيير في الوضع. أرسل /mode live لبدء طلب جديد."
            pending.pop(chat_id, None)
            try:
                self._activate_mode("live")
            except (ValueError, RuntimeError) as exc:
                return f"تعذر تفعيل Live: {exc}؛ بقي الوضع كما هو."
            return "تم تأكيد LIVE لهذه الجلسة فقط. قد يرسل البوت أوامر حقيقية؛ أرسل /mode paper للعودة فورًا إلى المحاكاة المحلية."

        if action == "cancel":
            getattr(self, "_pending_live_confirmations", {}).pop(chat_id, None)
            return "أُلغي طلب التحويل إلى Live؛ لم يتغير الوضع."
        return "الاستخدام: /mode أو /mode paper أو /mode demo أو /mode live. يتطلب Live تأكيدًا ثانيًا."

    def _activate_mode(self, mode: str) -> None:
        if mode not in {"paper", "demo", "live"}:
            raise ValueError("وضع غير معروف.")
        if mode == "live":
            if not self.settings.live_trading_enabled or self.settings.live_trading_ack != "I_UNDERSTAND_LIVE_CFD_RISK":
                raise RuntimeError("بوابة Live غير مفعلة.")
            if not self.settings.capital_configured:
                raise RuntimeError("بيانات اعتماد Capital.com غير مكتملة.")
            client = self._broker_clients.get("live")
            if client is None:
                client = CapitalClient(self.settings, environment="live")
                self._broker_clients["live"] = client
            self.capital = client
        elif mode == "demo":
            if not self.settings.capital_configured:
                raise RuntimeError("بيانات Capital.com غير مهيأة.")
            self.capital = self._broker_clients.get("demo") or CapitalDemoClient(self.settings)
            self._broker_clients["demo"] = self.capital
        else:
            self.capital = self._broker_clients.get("demo")
            if self.capital is None and self.settings.capital_configured:
                self.capital = CapitalDemoClient(self.settings)
                self._broker_clients["demo"] = self.capital
        if self.market_stream:
            self.market_stream.stop()
        self.market_stream = None
        self.stream_cache = StreamCache()
        self._last_multiframe_candidates.clear()
        self._market_cache = None
        self.active_mode = mode
        self._mode_armed = mode in {"demo", "live"}


    @staticmethod
    def _sentiment_text(scores: SentimentScores) -> str:
        return (
            f"تحليل FinBERT على CPU\nالتصنيف: {scores.label}\n"
            f"Positive: {scores.positive:.3f}\n"
            f"Negative: {scores.negative:.3f}\n"
            f"Neutral: {scores.neutral:.3f}\n"
            "هذه درجات تصنيف للنص وليست توصية أو توقعًا مضمونًا للسوق."
        )

    def _analysis_message(self, item: NewsItem, scores: SentimentScores) -> str:
        return f"{item.headline[:260]}\n{self._sentiment_text(scores)}"

    @staticmethod
    def _signal_message(signal: TradeSignal, *, simulated: bool) -> str:
        label = "إشارة ورقية؛ لم يُرسل أمر" if simulated else "إشارة"
        return (
            f"{label}: {signal.direction} {signal.market_name} "
            f"| أساس العملة {signal.currency_basis} "
            f"| الثقة {signal.sentiment.confidence:.3f}"
        )

    def _notify(self, text: str, chat_id: str | None = None) -> None:
        logger.info("%s", text.replace("\n", " | "))
        if not self.telegram:
            return
        try:
            self.telegram.send(text, chat_id=chat_id)
        except ApiError:
            logger.warning("Telegram message could not be sent.")
