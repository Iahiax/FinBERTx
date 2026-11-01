from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


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
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
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

    def summary(self) -> dict[str, int]:
        with self._connect() as db:
            seen = db.execute("SELECT COUNT(*) AS total FROM seen_news").fetchone()
            signals = db.execute(
                "SELECT COUNT(*) AS total FROM events WHERE kind = 'signal'"
            ).fetchone()
            orders = db.execute(
                "SELECT COUNT(*) AS total FROM events WHERE kind = 'demo_order'"
            ).fetchone()
        return {
            "news_analyzed": int(seen["total"]),
            "signals": int(signals["total"]),
            "demo_orders": int(orders["total"]),
        }
