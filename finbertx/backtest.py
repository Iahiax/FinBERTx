from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BacktestReport:
    trades: int
    wins: int
    losses: int
    win_rate_percent: float
    net_return_percent: float
    max_drawdown_percent: float
    profit_factor: float
    starting_balance: float
    final_balance: float
    leverage_used: float

    def as_text(self) -> str:
        factor = "∞" if self.profit_factor == float("inf") else f"{self.profit_factor:.2f}"
        return (
            "تقرير الاختبار التاريخي (محاكاة افتراضية وليست توقعًا):\n"
            f"الصفقات: {self.trades} | الرابحة: {self.wins} | الخاسرة: {self.losses}\n"
            f"نسبة الفوز: {self.win_rate_percent:.1f}%\n"
            f"الرصيد: {self.starting_balance:.2f} → {self.final_balance:.2f}\n"
            f"العائد المركب: {self.net_return_percent:.2f}%\n"
            f"أقصى تراجع: {self.max_drawdown_percent:.2f}%\n"
            f"عامل الربح: {factor} | الرافعة المفترضة: {self.leverage_used:.2f}:1"
        )


def run_backtest(
    csv_path: str | Path,
    *,
    starting_balance: float = 10_000.0,
    max_leverage: float = 1.0,
    risk_fraction: float = 0.01,
    stop_distance_percent: float = 0.5,
) -> BacktestReport:
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"Backtest CSV not found: {path}")
    if starting_balance <= 0:
        raise ValueError("Starting balance must be greater than zero.")
    if not 1 <= max_leverage <= 100:
        raise ValueError("Backtest leverage must be between 1 and 100.")
    if not 0 < risk_fraction <= 0.02 or not 0 < stop_distance_percent <= 2:
        raise ValueError("Risk fraction or stop distance is outside safe limits.")
    stop_fraction = stop_distance_percent / 100.0
    leverage_used = min(max_leverage, risk_fraction / stop_fraction)

    returns: list[float] = []
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required = {"timestamp", "epic", "direction", "entry_price", "exit_price"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(
                "CSV must include: timestamp, epic, direction, entry_price, exit_price"
            )
        for row in reader:
            direction = row["direction"].strip().upper()
            entry = float(row["entry_price"])
            exit_price = float(row["exit_price"])
            if direction not in {"BUY", "SELL"} or entry <= 0 or exit_price <= 0:
                raise ValueError("Invalid direction or non-positive price in backtest CSV.")
            raw_return = (exit_price - entry) / entry
            returns.append(raw_return if direction == "BUY" else -raw_return)
    if not returns:
        raise ValueError("Backtest CSV contains no trades.")

    equity = float(starting_balance)
    peak = equity
    max_drawdown = 0.0
    wins = losses = 0
    gross_gain = gross_loss = 0.0
    processed = 0
    for raw_return in returns:
        pnl = equity * leverage_used * raw_return
        equity = max(0.0, equity + pnl)
        processed += 1
        if pnl > 0:
            wins += 1
            gross_gain += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += abs(pnl)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
        if equity <= 0:
            break
    profit_factor = gross_gain / gross_loss if gross_loss else float("inf")
    return BacktestReport(
        trades=processed,
        wins=wins,
        losses=losses,
        win_rate_percent=100 * wins / processed,
        net_return_percent=(equity / starting_balance - 1) * 100,
        max_drawdown_percent=max_drawdown * 100,
        profit_factor=profit_factor,
        starting_balance=float(starting_balance),
        final_balance=equity,
        leverage_used=leverage_used,
    )
