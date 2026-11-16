from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable

from finbertx.capital import SUPPORTED_RESOLUTIONS
from finbertx.settings import Settings


@dataclass(frozen=True)
class TimeframeResult:
    resolution: str
    status: str
    candles: int = 0
    first_time: str = ""
    last_time: str = ""
    trades: int = 0
    wins: int = 0
    net_profit: float = 0.0
    net_return_percent: float = 0.0
    max_drawdown_percent: float = 0.0
    final_balance: float = 0.0
    history_complete: bool = True

    def as_text(self, currency: str) -> str:
        if self.status != "ok":
            detail = {
                "no_data": "لا توجد بيانات تاريخية لهذا الإطار.",
                "insufficient": f"بيانات غير كافية ({self.candles} شمعة؛ يلزم 22 على الأقل).",
                "error": "تعذر جلب بيانات هذا الإطار؛ لا توجد نتيجة محسوبة.",
            }.get(self.status, self.status)
            return f"{self.resolution}: {detail}"
        completeness = "" if self.history_complete else "\nالعينة محدودة: بلغ التنزيل حد الصفحات أو تعذرت متابعة التاريخ."
        return (
            f"{self.resolution}: {self.candles} شمعة ({self.first_time} → {self.last_time})\n"
            f"صفقات {self.trades}؛ فائزة {self.wins}؛ صافي {self.net_profit:+.2f} {currency} "
            f"({self.net_return_percent:+.2f}%)؛ أقصى تراجع {self.max_drawdown_percent:.2f}%"
            f"{completeness}"
        )


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _side_price(row: dict, field: str, side: str, fallback: float | None = None) -> float | None:
    prices = row.get(field) or {}
    keys = ("ask", "bid") if side == "ask" else ("bid", "ask")
    for key in keys:
        value = _number(prices.get(key))
        if value is not None and value > 0:
            return value
    return fallback


def _mid(row: dict) -> float | None:
    bid = _side_price(row, "closePrice", "bid")
    ask = _side_price(row, "closePrice", "ask")
    if bid and ask:
        return (bid + ask) / 2
    return bid or ask


def _sma(values: list[float], end: int, period: int) -> float:
    return sum(values[end - period + 1 : end + 1]) / period


def _normalise_candles(rows: list[dict]) -> list[dict]:
    by_time: dict[str, dict] = {}
    for row in rows:
        stamp = str(row.get("snapshotTimeUTC") or row.get("snapshotTime") or "")
        if stamp and _mid(row) is not None:
            by_time[stamp] = row
    return [by_time[stamp] for stamp in sorted(by_time)]


def backtest_timeframe(
    epic: str,
    resolution: str,
    rows: list[dict],
    *,
    starting_balance: float = 10_000.0,
    max_leverage: float = 100.0,
    max_epic_exposure_fraction: float = 0.25,
    risk_fraction: float = 0.01,
    stop_distance_percent: float = 0.5,
    take_profit_multiple: float = 2.0,
    fast_period: int = 5,
    slow_period: int = 20,
    currency: str = "USD",
    history_complete: bool = True,
) -> TimeframeResult:
    candles = _normalise_candles(rows)
    if not candles:
        return TimeframeResult(resolution, "no_data")
    if len(candles) < slow_period + 2:
        return TimeframeResult(resolution, "insufficient", candles=len(candles))

    mids = [_mid(row) or 0.0 for row in candles]
    balance = float(starting_balance)
    peak_equity = balance
    max_drawdown = 0.0
    position: dict | None = None
    trades = wins = 0
    stop_fraction = stop_distance_percent / 100.0

    def close_position(price: float) -> None:
        nonlocal balance, position, trades, wins
        if position is None or price <= 0:
            return
        direction = int(position["direction"])
        pnl = float(position["notional"]) * direction * (price - float(position["entry"])) / float(position["entry"])
        balance = max(0.0, balance + pnl)
        trades += 1
        wins += int(pnl > 0)
        position = None

    for i in range(slow_period + 1, len(candles)):
        # Only use completed candles up to i-1; entries happen at candle i open.
        prev_fast = _sma(mids, i - 2, fast_period)
        prev_slow = _sma(mids, i - 2, slow_period)
        signal_fast = _sma(mids, i - 1, fast_period)
        signal_slow = _sma(mids, i - 1, slow_period)
        desired = 1 if prev_fast <= prev_slow and signal_fast > signal_slow else (
            -1 if prev_fast >= prev_slow and signal_fast < signal_slow else 0
        )
        row = candles[i]
        if desired and position and int(position["direction"]) != desired:
            side = "bid" if int(position["direction"]) > 0 else "ask"
            open_prices = row.get("openPrice") or {}
            exit_at_open = _number(open_prices.get(side)) or _mid(row)
            if exit_at_open:
                close_position(exit_at_open)

        if desired and position is None and balance > 0:
            side = "ask" if desired > 0 else "bid"
            open_prices = row.get("openPrice") or {}
            entry = _number(open_prices.get(side)) or _mid(row)
            if entry and entry > 0:
                notional = min(
                    balance * max_leverage,
                    balance * max_epic_exposure_fraction,
                    balance * risk_fraction / max(stop_fraction, 1e-9),
                )
                distance = entry * stop_fraction
                position = {
                    "direction": desired,
                    "entry": entry,
                    "notional": notional,
                    "stop": entry - distance if desired > 0 else entry + distance,
                    "target": entry + distance * take_profit_multiple if desired > 0 else entry - distance * take_profit_multiple,
                }

        if position:
            direction = int(position["direction"])
            hi = row.get("highPrice") or {}
            lo = row.get("lowPrice") or {}
            # Long exits use bid, short exits use ask. If stop and target both fall
            # inside one candle, assume the adverse stop was hit first.
            if direction > 0:
                low = _number(lo.get("bid")) or _number(lo.get("ask"))
                high = _number(hi.get("bid")) or _number(hi.get("ask"))
                if low is not None and low <= position["stop"]:
                    close_position(float(position["stop"]))
                elif high is not None and high >= position["target"]:
                    close_position(float(position["target"]))
            else:
                high = _number(hi.get("ask")) or _number(hi.get("bid"))
                low = _number(lo.get("ask")) or _number(lo.get("bid"))
                if high is not None and high >= position["stop"]:
                    close_position(float(position["stop"]))
                elif low is not None and low <= position["target"]:
                    close_position(float(position["target"]))

        if position:
            side = "bid" if int(position["direction"]) > 0 else "ask"
            close_prices = row.get("closePrice") or {}
            mark = _number(close_prices.get(side)) or mids[i]
            floating = float(position["notional"]) * int(position["direction"]) * (mark - float(position["entry"])) / float(position["entry"])
            equity = max(0.0, balance + floating)
        else:
            equity = balance
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity * 100.0)

    if position:
        last = candles[-1]
        side = "bid" if int(position["direction"]) > 0 else "ask"
        close_prices = last.get("closePrice") or {}
        close_at = _number(close_prices.get(side)) or mids[-1]
        close_position(close_at)
        peak_equity = max(peak_equity, balance)
        if peak_equity > 0:
            max_drawdown = max(max_drawdown, (peak_equity - balance) / peak_equity * 100.0)

    initial = float(starting_balance)
    first_time = str(candles[0].get("snapshotTimeUTC") or candles[0].get("snapshotTime") or "?")
    last_time = str(candles[-1].get("snapshotTimeUTC") or candles[-1].get("snapshotTime") or "?")
    return TimeframeResult(
        resolution=resolution,
        status="ok",
        candles=len(candles),
        first_time=first_time,
        last_time=last_time,
        trades=trades,
        wins=wins,
        net_profit=balance - initial,
        net_return_percent=(balance / initial - 1) * 100 if initial else 0.0,
        max_drawdown_percent=max_drawdown,
        final_balance=balance,
        history_complete=history_complete,
    )


def run_all_timeframes(
    epic: str,
    fetch_prices: Callable[[str, str, int], list[dict] | tuple[list[dict], bool]],
    settings: Settings,
    *,
    maximum: int = 1000,
) -> list[TimeframeResult]:
    results: list[TimeframeResult] = []
    for resolution in SUPPORTED_RESOLUTIONS:
        try:
            fetched = fetch_prices(epic, resolution, maximum)
            if isinstance(fetched, tuple) and len(fetched) == 2:
                rows, history_complete = fetched
            else:
                rows, history_complete = fetched, True
            results.append(
                backtest_timeframe(
                    epic,
                    resolution,
                    rows,
                    starting_balance=settings.paper_starting_balance,
                    max_leverage=settings.paper_max_leverage,
                    max_epic_exposure_fraction=settings.max_epic_exposure_fraction,
                    risk_fraction=settings.paper_risk_fraction,
                    stop_distance_percent=settings.stop_distance_percent,
                    take_profit_multiple=settings.take_profit_multiple,
                    currency=settings.paper_account_currency,
                    history_complete=history_complete,
                )
            )
        except Exception:
            # Keep failures explicit per timeframe; do not fabricate a result.
            results.append(TimeframeResult(resolution, "error"))
    return results


def report_text(epic: str, results: list[TimeframeResult], settings: Settings) -> str:
    parts = [
        f"باكتيست OHLC للسوق {epic} — تقاطع SMA 5/20، رصيد ابتدائي "
        f"{settings.paper_starting_balance:.2f} {settings.paper_account_currency}، "
        f"مخاطرة المحاكاة {settings.paper_risk_fraction:.2%}، وقف "
        f"{settings.stop_distance_percent:.2f}% وهدف {settings.take_profit_multiple:g}R؛ "
        f"التعرض الأقصى للأداة {settings.max_epic_exposure_fraction:.0%}. "
        "يُجمع التاريخ على صفحات بحد 1,000 شمعة للطلب و100 صفحة كحد محلي لكل إطار.",
        "تنبيه: هذا معيار فني سعري وليس اختبارًا لنموذج FinBERT الإخباري؛ لا يوفر الوسيط تاريخ الأخبار اللازم لإعادة تشغيل تحليل المشاعر كما حدث وقتها. لا يحاكي حد خسارة اليوم أو كل تكاليف التداول (التمويل والعمولات والانزلاق)، ويُفترض تفعيل الوقف قبل الهدف إذا لم يُعرف ترتيب الأسعار داخل الشمعة.",
    ]
    parts.extend(result.as_text(settings.paper_account_currency) for result in results)
    return "\n\n".join(parts)
