from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from finbertx.journal import Journal
from finbertx.types import Market, TradeSignal


@dataclass(frozen=True)
class PaperFill:
    position_id: str
    epic: str
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    notional: float
    leverage: float
    account_currency: str


@dataclass(frozen=True)
class PaperClose:
    epic: str
    direction: str
    exit_price: float
    pnl: float
    balance: float
    reason: str


@dataclass(frozen=True)
class OpenResult:
    fill: PaperFill | None
    reason: str = ""


class PaperTradingEngine:
    """Local-only paper ledger. It never submits orders to a broker."""

    def __init__(
        self,
        journal: Journal,
        *,
        starting_balance: float,
        account_currency: str,
        max_leverage: float,
        risk_fraction: float,
        stop_distance_percent: float,
        take_profit_multiple: float,
        max_open_positions: int,
        max_daily_trades: int,
        daily_loss_limit_fraction: float = 0.03,
        max_epic_exposure_fraction: float = 0.25,
    ) -> None:
        self.journal = journal
        self.starting_balance = float(starting_balance)
        self.account_currency = account_currency.upper()
        self.max_leverage = min(float(max_leverage), 100.0)
        self.risk_fraction = float(risk_fraction)
        self.stop_fraction = float(stop_distance_percent) / 100.0
        self.take_profit_multiple = float(take_profit_multiple)
        self.max_open_positions = int(max_open_positions)
        self.max_daily_trades = int(max_daily_trades)
        self.daily_loss_limit_fraction = float(daily_loss_limit_fraction)
        self.max_epic_exposure_fraction = float(max_epic_exposure_fraction)
        self.journal.paper_balance(self.starting_balance)

    def balance(self) -> float:
        return self.journal.paper_balance(self.starting_balance)

    def equity(self, markets: dict[str, Market] | None = None) -> float:
        balance = self.balance()
        if not markets:
            return balance
        floating = 0.0
        for row in self.journal.open_paper_positions():
            market = markets.get(str(row["epic"]))
            if not market or market.market_status != "TRADEABLE":
                continue
            mark = self._exit_price(str(row["direction"]), market)
            if mark is None:
                continue
            signed_return = (mark - float(row["entry_price"])) / float(row["entry_price"])
            if str(row["direction"]) == "SELL":
                signed_return *= -1
            floating += float(row["notional"]) * signed_return
        return max(0.0, balance + floating)

    def daily_loss_status(
        self, markets: dict[str, Market] | None = None
    ) -> dict[str, float | bool | str]:
        current_equity = self.equity(markets)
        day = datetime.now(UTC).date().isoformat()
        start_equity = self.journal.paper_day_start_equity(day, current_equity)
        limit = max(0.0, start_equity) * self.daily_loss_limit_fraction
        pnl = current_equity - start_equity
        return {
            "utc_day": day,
            "start_equity": start_equity,
            "equity": current_equity,
            "pnl": pnl,
            "loss_limit": limit,
            "tripped": start_equity <= 0 or pnl <= -limit,
        }

    def open_signal(
        self,
        signal: TradeSignal,
        market: Market,
        *,
        signal_id: str,
        markets: dict[str, Market] | None = None,
    ) -> OpenResult:
        if market.market_status != "TRADEABLE":
            return OpenResult(None, "السوق مغلق أو غير قابل للتداول")
        if market.bid is None or market.offer is None or market.bid <= 0 or market.offer <= 0:
            return OpenResult(None, "لا تتوفر أسعار عرض وطلب صالحة")
        if signal.direction not in {"BUY", "SELL"}:
            return OpenResult(None, "اتجاه الإشارة غير صالح")
        open_positions = self.journal.open_paper_positions()
        if len(open_positions) >= self.max_open_positions:
            return OpenResult(None, "تم بلوغ حد المراكز المفتوحة")
        if any(row["epic"] == signal.epic for row in open_positions):
            return OpenResult(None, "يوجد مركز ورقي مفتوح على هذا السوق")
        if self.journal.paper_trades_today() >= self.max_daily_trades:
            return OpenResult(None, "تم بلوغ حد الصفقات الورقية اليومية")
        if self.journal.paper_signal_seen(signal_id):
            return OpenResult(None, "تمت معالجة هذه الإشارة مسبقًا")

        current_equity = self.equity(markets)
        if current_equity <= 0:
            return OpenResult(None, "الرصيد الورقي الحالي غير موجب")
        day = datetime.now(UTC).date().isoformat()
        start_equity = self.journal.paper_day_start_equity(day, current_equity)
        daily_pnl = current_equity - start_equity
        daily_loss_limit = max(0.0, start_equity) * self.daily_loss_limit_fraction
        if start_equity <= 0 or daily_pnl <= -daily_loss_limit:
            return OpenResult(None, "تم بلوغ حد الخسارة اليومية الورقية؛ أُوقف فتح مراكز جديدة")
        entry = market.offer if signal.direction == "BUY" else market.bid
        risk_amount = current_equity * self.risk_fraction
        risk_sized_notional = risk_amount / self.stop_fraction
        epic_exposure = sum(
            float(row["notional"])
            for row in open_positions
            if row["epic"] == signal.epic
        )
        max_epic_exposure = current_equity * self.max_epic_exposure_fraction
        remaining_epic_exposure = max_epic_exposure - epic_exposure
        if remaining_epic_exposure <= 0:
            return OpenResult(None, "تم بلوغ حد التعرض الاسمي لهذه الأداة")
        notional = min(
            risk_sized_notional,
            current_equity * self.max_leverage,
            remaining_epic_exposure,
        )
        if notional <= 0:
            return OpenResult(None, "تعذر حساب حجم افتراضي موجب")

        distance = entry * self.stop_fraction
        if signal.direction == "BUY":
            stop = entry - distance
            target = entry + distance * self.take_profit_multiple
        else:
            stop = entry + distance
            target = entry - distance * self.take_profit_multiple
        leverage = notional / current_equity
        position_id = uuid4().hex
        opened_at = datetime.now(UTC).isoformat()
        stored = self.journal.open_paper_position(
            position_id=position_id,
            signal_id=signal_id,
            epic=signal.epic,
            market_name=signal.market_name,
            direction=signal.direction,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            notional=notional,
            leverage=leverage,
            account_currency=self.account_currency,
            opened_at=opened_at,
        )
        if not stored:
            return OpenResult(None, "إشارة مكررة؛ لم يُفتح مركز جديد")
        return OpenResult(
            PaperFill(
                position_id=position_id,
                epic=signal.epic,
                direction=signal.direction,
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                notional=notional,
                leverage=leverage,
                account_currency=self.account_currency,
            )
        )

    def update_positions(self, markets: dict[str, Market]) -> list[PaperClose]:
        closed: list[PaperClose] = []
        for row in self.journal.open_paper_positions():
            market = markets.get(str(row["epic"]))
            if not market or market.market_status != "TRADEABLE":
                continue
            direction = str(row["direction"])
            exit_price = self._exit_price(direction, market)
            if exit_price is None:
                continue
            stop = float(row["stop_price"])
            target = float(row["target_price"])
            if direction == "BUY":
                reason = "stop" if exit_price <= stop else "target" if exit_price >= target else ""
            else:
                reason = "stop" if exit_price >= stop else "target" if exit_price <= target else ""
            if not reason:
                continue
            entry = float(row["entry_price"])
            signed_return = (exit_price - entry) / entry
            if direction == "SELL":
                signed_return *= -1
            pnl = float(row["notional"]) * signed_return
            new_balance = max(0.0, self.balance() + pnl)
            done = self.journal.close_paper_position(
                position_id=str(row["position_id"]),
                exit_price=exit_price,
                pnl=pnl,
                closed_at=datetime.now(UTC).isoformat(),
                reason=reason,
                new_balance=new_balance,
            )
            if done:
                closed.append(
                    PaperClose(
                        epic=str(row["epic"]),
                        direction=direction,
                        exit_price=exit_price,
                        pnl=pnl,
                        balance=new_balance,
                        reason=reason,
                    )
                )
        return closed

    def summary(self, markets: dict[str, Market] | None = None) -> dict[str, float | int | str]:
        stats = self.journal.paper_summary()
        return {
            **stats,
            "balance": self.balance(),
            "equity": self.equity(markets),
            "max_leverage": self.max_leverage,
            "max_epic_exposure_fraction": self.max_epic_exposure_fraction,
            "daily_loss_limit_fraction": self.daily_loss_limit_fraction,
            "account_currency": self.account_currency,
        }

    @staticmethod
    def _exit_price(direction: str, market: Market) -> float | None:
        # Buy positions close at bid; sells close at offer, including spread impact.
        value = market.bid if direction == "BUY" else market.offer
        return float(value) if value is not None and value > 0 else None
