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

    def as_text(self) -> str:
        return (
            "تقرير الاختبار التاريخي (نتائج نسبية وليست توقعًا):\n"
            f"الصفقات: {self.trades} | الرابحة: {self.wins} | الخاسرة: {self.losses}\n"
            f"نسبة الفوز: {self.win_rate_percent:.1f}%\n"
            f"العائد التراكمي: {self.net_return_percent:.2f}%\n"
            f"أقصى تراجع: {self.max_drawdown_percent:.2f}%\n"
            f"عامل الربح: {self.profit_factor:.2f}"
        )


def run_backtest(csv_path: str | Path) -> BacktestReport:
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"Backtest CSV not found: {path}")

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

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for trade_return in returns:
        equity *= 1 + trade_return
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    gains = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    gross_gain = sum(gains)
    gross_loss = abs(sum(losses))
    profit_factor = gross_gain / gross_loss if gross_loss else float("inf")
    return BacktestReport(
        trades=len(returns),
        wins=len(gains),
        losses=len(losses),
        win_rate_percent=100 * len(gains) / len(returns),
        net_return_percent=(equity - 1) * 100,
        max_drawdown_percent=max_drawdown * 100,
        profit_factor=profit_factor,
    )
