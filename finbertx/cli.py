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
        description="CPU-only FinBERT analysis with Capital.com demo safeguards.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="Analyze a financial headline.")
    analyze.add_argument("text", nargs="+")
    backtest = commands.add_parser("backtest", help="Analyze a historical CSV.")
    backtest.add_argument("--csv", required=True)
    commands.add_parser("check-config", help="Validate environment settings.")
    commands.add_parser("run", help="Start the Telegram/news polling service.")
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
            print(run_backtest(args.csv).as_text())
        elif args.command == "check-config":
            settings = Settings.from_env()
            settings.validate_for_run()
            print(
                "Configuration OK. "
                f"Capital host: demo only; orders enabled: {settings.enable_demo_orders}; "
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
