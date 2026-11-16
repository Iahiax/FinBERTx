from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_news (
                    news_id TEXT PRIMARY KEY,
                    seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    epic TEXT,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    epic TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS broker_epic_locks (
                    epic TEXT PRIMARY KEY,
                    attempt_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    position_id TEXT PRIMARY KEY,
                    signal_id TEXT UNIQUE NOT NULL,
                    epic TEXT NOT NULL,
                    market_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    notional REAL NOT NULL,
                    leverage REAL NOT NULL,
                    account_currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    exit_price REAL,
                    pnl REAL,
                    closed_at TEXT,
                    close_reason TEXT
                );
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(broker_attempts)")
            }
            if "attempt_id" not in columns:
                db.execute("ALTER TABLE broker_attempts RENAME TO broker_attempts_legacy")
                db.execute(
                    """CREATE TABLE broker_attempts (
                        attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        epic TEXT NOT NULL,
                        environment TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        detail TEXT NOT NULL DEFAULT ''
                    )"""
                )
                db.execute(
                    """INSERT INTO broker_attempts(epic, environment, created_at, status, detail)
                       SELECT epic, environment, created_at, status, detail
                       FROM broker_attempts_legacy"""
                )
                db.execute("DROP TABLE broker_attempts_legacy")
            # A process that restarted while an order was being submitted cannot
            # know whether the broker received it; preserve its lock as ambiguous.
            db.execute(
                "UPDATE broker_attempts SET status = 'AMBIGUOUS', "
                "detail = 'process restarted during submit' WHERE status = 'SUBMITTING'"
            )
            db.execute(
                """INSERT OR IGNORE INTO broker_epic_locks(epic, attempt_id)
                   SELECT epic, MAX(attempt_id) FROM broker_attempts
                   WHERE status = 'AMBIGUOUS' GROUP BY epic"""
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def mark_news_seen(self, news_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO seen_news(news_id, seen_at) VALUES (?, ?)",
                (news_id, datetime.now(UTC).isoformat()),
            )
            return cursor.rowcount == 1

    def add_event(self, kind: str, detail: str, epic: str = "") -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO events(created_at, kind, epic, detail) VALUES (?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), kind, epic, detail[:500]),
            )

    def set_value(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_value(self, key: str, default: str = "") -> str:
        with self._connect() as db:
            row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    @property
    def paused(self) -> bool:
        return self.get_value("paused", "false").lower() == "true"

    def set_paused(self, paused: bool) -> None:
        self.set_value("paused", "true" if paused else "false")

    def daily_demo_order_count(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS total FROM events "
                "WHERE kind = 'demo_order' AND substr(created_at, 1, 10) = ?",
                (today,),
            ).fetchone()
        return int(row["total"])

    def daily_broker_order_count(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS total FROM broker_attempts "
                "WHERE substr(created_at, 1, 10) = ?",
                (today,),
            ).fetchone()
        return int(row["total"])

    def broker_attempt_status(self, epic: str) -> str:
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM broker_attempts WHERE epic = ? "
                "ORDER BY attempt_id DESC LIMIT 1", (epic,)
            ).fetchone()
        return str(row["status"]) if row else ""

    def broker_epic_locked(self, epic: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM broker_epic_locks WHERE epic = ?", (epic,)
            ).fetchone()
        return row is not None

    def reserve_broker_attempt(
        self, epic: str, environment: str, daily_limit: int
    ) -> str:
        """Atomically count and lock an EPIC immediately before a broker POST.

        Every POST attempt is stored and counted. The EPIC lock remains after an
        ambiguous result/restart, and is released only after confirmed success.
        """
        today = datetime.now(UTC).date().isoformat()
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            locked = db.execute(
                "SELECT 1 FROM broker_epic_locks WHERE epic = ?", (epic,)
            ).fetchone()
            if locked:
                return "locked"
            count = db.execute(
                "SELECT COUNT(*) AS total FROM broker_attempts "
                "WHERE substr(created_at, 1, 10) = ?",
                (today,),
            ).fetchone()
            if int(count["total"]) >= daily_limit:
                return "daily_limit"
            cursor = db.execute(
                "INSERT INTO broker_attempts(epic, environment, created_at, status) "
                "VALUES (?, ?, ?, 'SUBMITTING')",
                (epic, environment, created_at),
            )
            attempt_id = int(cursor.lastrowid)
            db.execute(
                "INSERT INTO broker_epic_locks(epic, attempt_id) VALUES (?, ?)",
                (epic, attempt_id),
            )
            db.execute(
                "INSERT INTO events(created_at, kind, epic, detail) VALUES (?, ?, ?, ?)",
                (created_at, "broker_order_attempt", epic, f"{environment} POST reserved"),
            )
            return "reserved"

    def mark_broker_attempt_confirmed(self, epic: str, detail: str = "") -> None:
        with self._connect() as db:
            lock = db.execute(
                "SELECT attempt_id FROM broker_epic_locks WHERE epic = ?", (epic,)
            ).fetchone()
            if not lock:
                return
            attempt_id = int(lock["attempt_id"])
            db.execute(
                "UPDATE broker_attempts SET status = 'CONFIRMED', detail = ? "
                "WHERE attempt_id = ?",
                (detail[:300], attempt_id),
            )
            db.execute(
                "DELETE FROM broker_epic_locks WHERE epic = ? AND attempt_id = ?",
                (epic, attempt_id),
            )

    def mark_broker_attempt_ambiguous(self, epic: str, detail: str = "") -> None:
        with self._connect() as db:
            lock = db.execute(
                "SELECT attempt_id FROM broker_epic_locks WHERE epic = ?", (epic,)
            ).fetchone()
            if not lock:
                return
            db.execute(
                "UPDATE broker_attempts SET status = 'AMBIGUOUS', detail = ? "
                "WHERE attempt_id = ?",
                (detail[:300], int(lock["attempt_id"])),
            )

    def confirmed_broker_order_count(self) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS total FROM broker_attempts WHERE status = 'CONFIRMED'"
            ).fetchone()
        return int(row["total"])

    def summary(self) -> dict[str, int]:
        with self._connect() as db:
            seen = db.execute("SELECT COUNT(*) AS total FROM seen_news").fetchone()
            signals = db.execute(
                "SELECT COUNT(*) AS total FROM events WHERE kind = 'signal'"
            ).fetchone()
            paper = db.execute(
                "SELECT COUNT(*) AS total FROM events WHERE kind = 'paper_open'"
            ).fetchone()
        return {
            "news_analyzed": int(seen["total"]),
            "signals": int(signals["total"]),
            "paper_trades": int(paper["total"]),
        }

    def paper_balance(self, starting_balance: float) -> float:
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM state WHERE key = 'paper_balance'"
            ).fetchone()
            if row:
                try:
                    return float(row["value"])
                except (TypeError, ValueError):
                    pass
            db.execute(
                "INSERT INTO state(key, value) VALUES ('paper_balance', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(float(starting_balance)),),
            )
            return float(starting_balance)

    def paper_day_start_equity(self, day: str, current_equity: float) -> float:
        """Persist the first observed paper equity for a UTC day."""
        key = f"paper_day_start_equity:{day}"
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO state(key, value) VALUES (?, ?)",
                (key, repr(float(current_equity))),
            )
            row = db.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Could not persist paper day-start equity.")
        return float(row["value"])

    def set_paper_balance(self, balance: float) -> None:
        self.set_value("paper_balance", repr(float(balance)))

    def open_paper_position(self, **position: Any) -> bool:
        with self._connect() as db:
            try:
                db.execute(
                    """INSERT INTO paper_positions (
                        position_id, signal_id, epic, market_name, direction,
                        entry_price, stop_price, target_price, notional, leverage,
                        account_currency, status, opened_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
                    (
                        position["position_id"], position["signal_id"],
                        position["epic"], position["market_name"], position["direction"],
                        position["entry_price"], position["stop_price"], position["target_price"],
                        position["notional"], position["leverage"], position["account_currency"],
                        position["opened_at"],
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            db.execute(
                "INSERT INTO events(created_at, kind, epic, detail) VALUES (?, 'paper_open', ?, ?)",
                (
                    position["opened_at"], position["epic"],
                    f"{position['direction']} {position['market_name']} "
                    f"notional={position['notional']:.2f} leverage={position['leverage']:.2f}x",
                ),
            )
            return True

    def paper_signal_seen(self, signal_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM paper_positions WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        return row is not None

    def open_paper_positions(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM paper_positions WHERE status = 'OPEN' ORDER BY opened_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def paper_trades_today(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS total FROM events "
                "WHERE kind = 'paper_open' AND substr(created_at, 1, 10) = ?",
                (today,),
            ).fetchone()
        return int(row["total"])

    def close_paper_position(
        self,
        *,
        position_id: str,
        exit_price: float,
        pnl: float,
        closed_at: str,
        reason: str,
        new_balance: float,
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE paper_positions
                   SET status = 'CLOSED', exit_price = ?, pnl = ?, closed_at = ?, close_reason = ?
                   WHERE position_id = ? AND status = 'OPEN'""",
                (exit_price, pnl, closed_at, reason, position_id),
            )
            if cursor.rowcount != 1:
                return False
            db.execute(
                "INSERT INTO state(key, value) VALUES ('paper_balance', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (repr(float(new_balance)),),
            )
            db.execute(
                "INSERT INTO events(created_at, kind, epic, detail) "
                "SELECT ?, 'paper_close', epic, ? FROM paper_positions WHERE position_id = ?",
                (closed_at, f"{reason}; pnl={pnl:.2f}; balance={new_balance:.2f}", position_id),
            )
            return True

    def paper_summary(self) -> dict[str, int | float]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS n, COALESCE(SUM(pnl), 0) AS pnl "
                "FROM paper_positions GROUP BY status"
            ).fetchall()
        summary: dict[str, int | float] = {
            "open_positions": 0,
            "closed_positions": 0,
            "realized_pnl": 0.0,
        }
        for row in rows:
            if row["status"] == "OPEN":
                summary["open_positions"] = int(row["n"])
            elif row["status"] == "CLOSED":
                summary["closed_positions"] = int(row["n"])
                summary["realized_pnl"] = float(row["pnl"])
        return summary
