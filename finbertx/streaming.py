from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Callable

from finbertx.capital import SUPPORTED_RESOLUTIONS

logger = logging.getLogger("finbertx.stream")
MAX_STREAM_EPICS = 40


def chunks(values: list[str], size: int = MAX_STREAM_EPICS) -> list[list[str]]:
    if size < 1:
        raise ValueError("chunk size must be positive")
    return [values[index : index + size] for index in range(0, len(values), size)]


def subscription_message(destination: str, cst: str, security_token: str, payload: dict | None = None) -> str:
    body = {
        "destination": destination,
        "correlationId": f"finbertx-{int(time.time() * 1000)}",
        "cst": cst,
        "securityToken": security_token,
    }
    if payload is not None:
        body["payload"] = payload
    return json.dumps(body, separators=(",", ":"))


class StreamCache:
    """Small in-memory cache of live quotes and OHLC bars; it never submits orders."""

    def __init__(self, max_bars_per_series: int = 1000):
        self._lock = threading.RLock()
        self.max_bars_per_series = max_bars_per_series
        self.quotes: dict[str, dict] = {}
        self.quote_updated_at: dict[str, float | None] = {}
        self.bars: dict[tuple[str, str], OrderedDict[str, dict]] = {}
        self.last_update_at: float | None = None

    def consume(self, message: dict) -> None:
        destination = str(message.get("destination") or "")
        payload = message.get("payload") or {}
        if not isinstance(payload, dict):
            return
        epic = str(payload.get("epic") or "")
        if not epic:
            return
        with self._lock:
            self.last_update_at = time.time()
            if destination == "quote":
                self.quotes[epic] = dict(payload)
                try:
                    source_time = float(payload.get("timestamp"))
                    if source_time > 100_000_000_000:
                        source_time /= 1000.0
                    if source_time < 1_000_000_000:
                        source_time = None
                except (TypeError, ValueError):
                    source_time = None
                self.quote_updated_at[epic] = source_time
            elif destination == "ohlc.event":
                resolution = str(payload.get("resolution") or "")
                raw_stamp = payload.get("t")
                if raw_stamp is None:
                    raw_stamp = payload.get("timestamp")
                stamp = str(raw_stamp) if raw_stamp is not None else ""
                if not resolution or not stamp:
                    return
                key = (epic, resolution)
                series = self.bars.setdefault(key, OrderedDict())
                # WebSocket may send updates to the still-forming bar. Replace
                # that timestamp instead of counting every tick as a new candle.
                series[stamp] = dict(payload)
                series.move_to_end(stamp)
                while len(series) > self.max_bars_per_series:
                    series.popitem(last=False)

    def snapshot(self) -> tuple[dict[str, dict], dict[tuple[str, str], list[dict]]]:
        with self._lock:
            return (
                {epic: dict(row) for epic, row in self.quotes.items()},
                {key: list(rows.values()) for key, rows in self.bars.items()},
            )

    def quote_age(self, epic: str, *, now: float | None = None) -> float | None:
        _, age = self.quote_snapshot(epic, now=now)
        return age

    def quote_snapshot(
        self, epic: str, *, now: float | None = None
    ) -> tuple[dict | None, float | None]:
        """Return a source quote and its age from the same cache instant."""
        with self._lock:
            row = self.quotes.get(epic)
            updated = self.quote_updated_at.get(epic)
            copied = dict(row) if row is not None else None
        if updated is None or copied is None:
            return copied, None
        age = (time.time() if now is None else now) - updated
        if age < -5:
            return copied, None
        return copied, max(0.0, age)

    def aligned_trend(self, epic: str, *, fast: int = 5, slow: int = 20) -> str | None:
        """Return a simple SMA alignment only after every supported frame has data."""
        with self._lock:
            directions: list[int] = []
            for resolution in SUPPORTED_RESOLUTIONS:
                series = self.bars.get((epic, resolution))
                closes: list[float] = []
                if series:
                    for bar in series.values():
                        try:
                            close = float(bar.get("c"))
                        except (TypeError, ValueError):
                            continue
                        if close > 0:
                            closes.append(close)
                if len(closes) < slow:
                    return None
                fast_mean = sum(closes[-fast:]) / fast
                slow_mean = sum(closes[-slow:]) / slow
                if fast_mean == slow_mean:
                    return None
                directions.append(1 if fast_mean > slow_mean else -1)
            if all(direction == 1 for direction in directions):
                return "BUY"
            if all(direction == -1 for direction in directions):
                return "SELL"
            return None


class CapitalWebSocketStream:
    """Rotating Capital.com WebSocket subscriptions; at most 40 epics per session."""

    def __init__(
        self,
        client,
        on_message: Callable[[dict], None],
        *,
        rotation_seconds: int = 30,
        socket_factory=None,
    ):
        if rotation_seconds < 5:
            raise ValueError("rotation_seconds must be at least 5")
        self.client = client
        self.on_message = on_message
        self.rotation_seconds = rotation_seconds
        self.socket_factory = socket_factory
        self._epics: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def update_epics(self, epics: list[str]) -> None:
        unique = list(dict.fromkeys(str(epic) for epic in epics if epic))
        with self._lock:
            self._epics = unique

    def start(self, epics: list[str]) -> None:
        self.update_epics(epics)
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="capital-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                epics = list(self._epics)
            batches = chunks(epics)
            if not batches:
                self._stop.wait(2)
                continue
            for batch in batches:
                if self._stop.is_set():
                    break
                try:
                    self._stream_batch(batch, rotate=len(batches) > 1)
                except Exception as exc:
                    logger.warning("Capital.com WebSocket cycle failed (%s).", type(exc).__name__)
                    self._stop.wait(2)

    def _stream_batch(self, epics: list[str], *, rotate: bool) -> None:
        factory = self.socket_factory
        if factory is None:
            import websocket

            factory = websocket.create_connection
        cst, security_token, endpoint = self.client.stream_credentials()
        socket = factory(endpoint, timeout=2)
        started = time.monotonic()
        last_ping = started
        try:
            socket.send(
                subscription_message(
                    "marketData.subscribe",
                    cst,
                    security_token,
                    {"epics": epics},
                )
            )
            socket.send(
                subscription_message(
                    "OHLCMarketData.subscribe",
                    cst,
                    security_token,
                    {"epics": epics, "resolutions": list(SUPPORTED_RESOLUTIONS), "type": "classic"},
                )
            )
            while not self._stop.is_set():
                now = time.monotonic()
                if rotate and now - started >= self.rotation_seconds:
                    break
                if not rotate and now - started >= 7 * 60:
                    # Refresh the session before Capital.com's 10-minute stream limit.
                    break
                if now - last_ping >= 8 * 60:
                    socket.send(subscription_message("ping", cst, security_token))
                    last_ping = now
                try:
                    raw = socket.recv()
                except TimeoutError:
                    continue
                except Exception as exc:
                    # websocket-client uses its own timeout exception class.
                    if type(exc).__name__ in {"WebSocketTimeoutException", "TimeoutError"}:
                        continue
                    raise
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(message, dict):
                    self.on_message(message)
        finally:
            try:
                socket.close()
            except Exception:
                pass
