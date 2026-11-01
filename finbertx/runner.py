from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from finbertx.backtest import run_backtest
from finbertx.capital import CapitalDemoClient
from finbertx.finbert import FinBertEngine
from finbertx.journal import Journal
from finbertx.network import ApiError
from finbertx.settings import Settings
from finbertx.sources import EconomicCalendar, FinnhubSource
from finbertx.strategy import currency_pair, is_in_high_impact_blackout, make_signal
from finbertx.telegram import TelegramBot
from finbertx.types import EconomicEvent, Market, NewsItem, TradeSignal

logger = logging.getLogger("finbertx")


class TradingBot:
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
        self.telegram = (
            TelegramBot(settings.telegram_bot_token, settings.telegram_chat_id)
            if settings.telegram_configured
            else None
        )
        self.engine = FinBertEngine()
        self.journal = Journal(settings.journal_path)
        self._market_cache: tuple[float, list[Market]] | None = None
        self._closed_notified: set[str] = set()

    def run_forever(self) -> None:
        mode = "read-only analysis and hypothetical paper signals"
        self._notify(f"بدأ FinBERTx على CPU. الوضع: {mode}.")
        while True:
            started = time.monotonic()
            try:
                self.poll_once()
                self._maybe_send_backtest()
            except Exception as exc:  # Keep the service alive; never print request URLs.
                logger.exception("Polling cycle failed: %s", type(exc).__name__)
                self._notify("تعذر إكمال دورة التحليل؛ لم يُرسل أي أمر جديد.")
            elapsed = time.monotonic() - started
            time.sleep(max(1, self.settings.poll_interval_seconds - elapsed))

    def poll_once(self) -> None:
        self._handle_telegram_updates()
        events: list[EconomicEvent] = []
        if self.calendar.providers:
            try:
                events = self.calendar.upcoming()
            except ApiError:
                if self.settings.enable_demo_orders:
                    self._notify(
                        "توقف فتح صفقات العرض مؤقتًا: تعذر تحديث المفكرة الاقتصادية."
                    )
                    return
                logger.warning("Calendar providers unavailable; analysis only.")

        items = self.news.forex_news(self.settings.max_news_per_cycle)
        markets = self._markets() if self.capital else []
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
        if self._market_cache and now - self._market_cache[0] < 300:
            return self._market_cache[1]
        markets = self.capital.all_markets()
        self._market_cache = (now, markets)
        return markets

    def _process_signals(
        self,
        item: NewsItem,
        scores,
        markets: list[Market],
        events: list[EconomicEvent],
    ) -> None:
        if not markets:
            return
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
            self._notify(self._signal_message(signal, simulated=True))

    def _handle_telegram_updates(self) -> None:
        if not self.telegram:
            return
        try:
            updates = self.telegram.updates()
        except ApiError:
            logger.warning("Telegram update poll failed.")
            return
        for update in updates:
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if chat_id not in self.settings.telegram_allowed_chat_ids:
                continue
            text = str(message.get("text") or "").strip()
            command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
            if command not in {
                "/help",
                "/status",
                "/pause",
                "/resume",
                "/backtest",
                "/signals",
            }:
                continue
            self._run_command(command, chat_id)

    def _run_command(self, command: str, reply_to: str) -> None:
        if command == "/help":
            text = (
                "الأوامر المتاحة:\n/status — الحالة\n/pause — إيقاف الصفقات\n"
                "/resume — استئناف التحليل\n/backtest — تقرير CSV\n"
                "/signals — ملخص الإشارات\n/help — المساعدة"
            )
        elif command == "/status":
            execution = "قراءة فقط ومحاكاة؛ لا تُرسل أوامر مالية"
            text = (
                f"الحالة: {'متوقف' if self.journal.paused else 'يعمل'}\n"
                f"التنفيذ: {execution}\n"
                f"الجهاز: CPU\n"
                f"الأزواج المكتشفة: {len(self._markets()) if self.capital else 0}\n"
                f"ملخص: {self.journal.summary()}"
            )
        elif command == "/pause":
            self.journal.set_paused(True)
            text = "أوقفت إرسال الصفقات. يستمر تحليل الأخبار."
        elif command == "/resume":
            self.journal.set_paused(False)
            text = (
                "استؤنف التحليل. "
                + "لن تُرسل صفقات؛ التشغيل محصور بالتحليل والمحاكاة."
            )
        elif command == "/signals":
            text = f"الملخص الحالي: {self.journal.summary()}"
        else:
            text = self._backtest_message()
        self._notify(text, chat_id=reply_to)

    def _maybe_send_backtest(self) -> None:
        path = self.settings.backtest_csv
        if not path or not Path(path).is_file():
            return
        previous = self.journal.get_value("last_backtest_at")
        try:
            previous_time = datetime.fromisoformat(previous) if previous else None
        except ValueError:
            previous_time = None
        due = (
            previous_time is None
            or (datetime.now(UTC) - previous_time).total_seconds()
            >= self.settings.backtest_report_interval_hours * 3600
        )
        if due:
            self._notify(self._backtest_message())
            self.journal.set_value("last_backtest_at", datetime.now(UTC).isoformat())

    def _backtest_message(self) -> str:
        if not self.settings.backtest_csv:
            return "لم يتم تحديد BACKTEST_CSV؛ لم أُنشئ نتائج افتراضية."
        try:
            return run_backtest(self.settings.backtest_csv).as_text()
        except (FileNotFoundError, ValueError) as exc:
            return f"تعذر تشغيل الاختبار التاريخي: {exc}"

    def _analysis_message(self, item: NewsItem, scores) -> str:
        return (
            f"تحليل FinBERT على CPU\n{item.headline[:260]}\n"
            f"النتيجة: {scores.label}\n"
            f"إيجابي {scores.positive:.3f} | سلبي {scores.negative:.3f} "
            f"| محايد {scores.neutral:.3f}"
        )

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
