from __future__ import annotations

import argparse
import json
import logging
import sys

from finbertx.backtest import run_backtest
from finbertx.finbert import FinBertEngine
from finbertx.runner import TradingBot
from finbertx.settings import ConfigurationError, Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finbertx",
        description="CPU-only FinBERT analysis with local paper-trading safeguards.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="Analyze financial text with FinBERT.")
    analyze.add_argument("text", nargs="+")
    backtest = commands.add_parser("backtest", help="Analyze a historical CSV.")
    backtest.add_argument("--csv", required=True)
    backtest.add_argument("--starting-balance", type=float)
    backtest.add_argument("--leverage", type=float)
    commands.add_parser("check-config", help="Validate environment settings.")
    commands.add_parser("run", help="Start the news and Telegram polling service.")
    return parser


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command == "analyze":
            scores = FinBertEngine().analyze(" ".join(args.text))
            print(
                json.dumps(
                    {
                        "device": "cpu",
                        "label": scores.label,
                        "positive": scores.positive,
                        "negative": scores.negative,
                        "neutral": scores.neutral,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "backtest":
            settings = Settings.from_env()
            report = run_backtest(
                args.csv,
                starting_balance=args.starting_balance or settings.paper_starting_balance,
                max_leverage=args.leverage or settings.paper_max_leverage,
                risk_fraction=settings.paper_risk_fraction,
                stop_distance_percent=settings.stop_distance_percent,
            )
            print(report.as_text())
        elif args.command == "check-config":
            settings = Settings.from_env()
            settings.validate_for_run()
            print(
                "Configuration OK. "
                f"Mode: {settings.trading_mode}; Capital data: demo only; "
                "broker orders: disabled; live trading: locked; "
                f"paper leverage ceiling: {settings.paper_max_leverage:.0f}:1; "
                f"Telegram configured: {settings.telegram_configured}."
            )
        elif args.command == "run":
            settings = Settings.from_env()
            TradingBot(settings).run_forever()
    except (ConfigurationError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
