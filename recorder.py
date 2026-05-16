"""FSU1B recorder — Betfair stream consumer + NDJSON writer.

The recorder is a tape recorder. It captures and stores. It does
not calculate anything.

Lifecycle:

1. ``Recorder.start()`` is called via ``POST /admin/control/start``:
   - Build Betfair client via auth.build_trading_client()
   - Subscribe to GB/IE WIN horse-racing markets
   - Determine current trading day, download any existing GCS file
     to /tmp so we can append (resume on restart)
   - Spawn three threads:
       1. WebSocket consumer (betfairlightweight's start() blocks)
       2. Queue drainer — pulls MarketBook batches, serialises to
          NDJSON, appends to in-memory buffer
       3. Flush worker — every ``flush_interval_seconds`` (or when
          buffer reaches ``flush_threshold_lines``), persists buffer
          to local file and uploads file to GCS
   - A daily rollover check inside the flush worker rotates the
     local file + writes the meta sidecar when the clock crosses
     the next 12:00 UTC boundary
2. ``Recorder.stop()`` is called via ``POST /admin/control/stop``:
   - Stop the stream
   - Flush remaining buffer
   - Write meta sidecar for the day being recorded
   - Clear all threads

No betting. No decisions. No calculations. Just capture.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Any, Optional

import betfairlightweight  # type: ignore[import-untyped]
from betfairlightweight import filters  # type: ignore[import-untyped]
from betfairlightweight.streaming import StreamListener  # type: ignore[import-untyped]

import gcs
from auth import build_trading_client
from settings import RecorderSettings

logger = logging.getLogger(__name__)


HORSE_RACING_EVENT_TYPE = "7"


# ──────────────────────────────────────────────────────────────────────────────
# MarketBook → NDJSON serialisation
# ──────────────────────────────────────────────────────────────────────────────


def market_book_to_ndjson_line(book) -> str:  # type: ignore[no-untyped-def]
    """Serialise a betfairlightweight MarketBook to a single NDJSON line.

    Field shape locked in the README — this is the contract. Do NOT
    add derived values here.
    """

    md = getattr(book, "market_definition", None)
    runners_md_index: dict[int, Any] = {}
    if md is not None:
        runners_md_index = {r.selection_id: r for r in (md.runners or [])}

    runners = []
    for r in (book.runners or []):
        meta = runners_md_index.get(r.selection_id)
        name = (
            getattr(meta, "name", None)
            or getattr(meta, "runner_name", None)
            or f"selection_{r.selection_id}"
        )
        back: list[list[float]] = []
        lay: list[list[float]] = []
        ex = getattr(r, "ex", None)
        if ex is not None:
            atb = (getattr(ex, "available_to_back", None) or [])[:3]
            atl = (getattr(ex, "available_to_lay", None) or [])[:3]
            back = [[float(p.price), float(p.size)] for p in atb]
            lay = [[float(p.price), float(p.size)] for p in atl]
        ltp = getattr(r, "last_price_traded", None)
        runners.append(
            {
                "sid": int(r.selection_id),
                "name": name,
                "ltp": float(ltp) if ltp is not None else None,
                "back": back,
                "lay": lay,
                "status": getattr(r, "status", "ACTIVE") or "ACTIVE",
            }
        )

    record = {
        "ts": _iso_now_ms(),
        "mid": str(getattr(book, "market_id", "")),
        "event": (getattr(md, "event_name", None) if md else None) or "",
        "name": (getattr(md, "name", None) if md else None) or "",
        "status": (getattr(md, "status", None) if md else None) or "OPEN",
        "inplay": bool((getattr(md, "in_play", False) if md else False)),
        "total_matched": float(getattr(book, "total_matched", 0) or 0),
        "runners": runners,
    }
    return json.dumps(record, separators=(",", ":"), default=str)


def _iso_now_ms() -> str:
    """ISO 8601 UTC with millisecond precision + Z suffix."""

    now = datetime.now(tz=timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ──────────────────────────────────────────────────────────────────────────────
# Recorder
# ──────────────────────────────────────────────────────────────────────────────


class Recorder:
    """Single-instance Betfair stream recorder.

    Thread-safe. One Recorder per Cloud Run container; the container
    is pinned to max-instances=1 so this invariant holds.
    """

    def __init__(self, settings: RecorderSettings) -> None:
        self._settings = settings
        self._trading: Optional[betfairlightweight.APIClient] = None
        self._stream = None
        self._stream_thread: Optional[threading.Thread] = None
        self._drain_thread: Optional[threading.Thread] = None
        self._flush_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()

        # State for the current trading day
        self._current_date: Optional[str] = None
        self._local_path: Optional[str] = None
        self._recording_start: Optional[str] = None

        # In-memory buffer of NDJSON lines awaiting flush
        self._buffer: list[str] = []
        self._buffer_lock = threading.Lock()

        # Stats
        self._updates_recorded = 0
        self._markets_tracked: set[str] = set()
        self._venues: set[str] = set()
        self._last_update_at: Optional[str] = None
        self._last_flush_at: Optional[str] = None
        self._updates_at_last_flush = 0
        self._updates_at_last_minute_check: int = 0
        self._last_minute_check: float = time.time()
        self._updates_per_minute: float = 0.0

        # Stream status
        self._stream_status = "DISCONNECTED"

    # ── State ──────────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return (
            self._stream_thread is not None
            and self._stream_thread.is_alive()
        )

    @property
    def settings(self) -> RecorderSettings:
        return self._settings

    def replace_settings(self, new_settings: RecorderSettings) -> None:
        self._settings = new_settings

    def status(self) -> dict[str, Any]:
        """Body for /admin/status."""

        local_size = 0
        if self._local_path and os.path.exists(self._local_path):
            local_size = os.path.getsize(self._local_path)
        return {
            "service": "fsu1b-stream-recorder",
            "fsu": "FSU1B",
            "version": "0.2.0",
            "shell_mode": False,
            "recording": self.is_recording,
            "auto_start": self._settings.auto_start,
            "stream_status": self._stream_status,
            "current_file": (
                gcs.gcs_ndjson_path(self._current_date)
                if self._current_date
                else None
            ),
            "current_date": self._current_date,
            "markets_tracked": len(self._markets_tracked),
            "updates_recorded": self._updates_recorded,
            "file_size_bytes": local_size,
            "recording_since": self._recording_start,
            "last_update_at": self._last_update_at,
            "last_flush_at": self._last_flush_at,
            "timestamp": _iso_now_ms(),
        }

    def stats(self) -> dict[str, Any]:
        """Body for /admin/stats."""

        local_size = 0
        if self._local_path and os.path.exists(self._local_path):
            local_size = os.path.getsize(self._local_path)
        return {
            "updates_per_minute": round(self._updates_per_minute, 1),
            "file_size_bytes": local_size,
            "markets_count": len(self._markets_tracked),
            "venues": sorted(self._venues),
            "last_update_at": self._last_update_at,
            "last_flush_at": self._last_flush_at,
            "buffer_pending": len(self._buffer),
            "timestamp": _iso_now_ms(),
        }

    # ── Public lifecycle ───────────────────────────────────────────────

    def start(self) -> dict[str, Any]:
        """Begin recording. Idempotent — second call returns current state."""

        if self.is_recording:
            return {"action": "start", "accepted": False, "detail": "already recording"}

        try:
            self._stop_flag.clear()
            self._trading = build_trading_client()
        except Exception as exc:  # noqa: BLE001
            logger.exception("FSU1B start failed at login: %s", exc)
            return {"action": "start", "accepted": False, "detail": f"login failed: {exc}"}

        # Determine trading day + restore any existing file
        self._current_date = gcs.trading_day_for()
        self._local_path = gcs.local_ndjson_path(self._current_date)
        try:
            resumed_bytes = gcs.download_existing(self._current_date)
            if resumed_bytes:
                logger.info(
                    "FSU1B resumed mid-day file (%d bytes already on disk)",
                    resumed_bytes,
                )
            else:
                # Ensure local file exists (empty)
                open(self._local_path, "a").close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("FSU1B resume-download failed (will start fresh): %s", exc)
            open(self._local_path, "a").close()

        self._recording_start = _iso_now_ms()

        # Spawn the stream thread (which spawns its own consumer + we
        # spawn the drainer + flush worker).
        self._stream_thread = threading.Thread(
            target=self._run_stream, name="fsu1b-stream", daemon=True,
        )
        self._stream_thread.start()
        return {"action": "start", "accepted": True, "detail": "FSU1B recording started"}

    def stop(self) -> dict[str, Any]:
        """Stop the recorder. Flushes remaining buffer + writes meta."""

        if not self.is_recording and not self._stream:
            return {"action": "stop", "accepted": False, "detail": "not recording"}

        self._stop_flag.set()

        # Stop the stream
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("stream.stop raised: %s", exc)
        self._stream_status = "DISCONNECTED"

        # Final flush of any pending buffer
        try:
            self._flush(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("final flush failed: %s", exc)

        # Write meta for this trading day
        if self._current_date:
            self._write_meta(closed=True)

        return {"action": "stop", "accepted": True, "detail": "FSU1B recording stopped"}

    def shutdown(self) -> None:
        """Called on app shutdown. Stops cleanly if running."""

        if self.is_recording or self._stream:
            self.stop()

    # ── Internals: stream + drain + flush ─────────────────────────────

    def _run_stream(self) -> None:
        try:
            self._stream_status = "CONNECTING"
            stream_queue: Queue = Queue()
            listener = StreamListener(output_queue=stream_queue)

            market_filter = filters.streaming_market_filter(
                event_type_ids=[HORSE_RACING_EVENT_TYPE],
                country_codes=list(self._settings.market_countries),
                market_types=list(self._settings.market_type_codes),
            )
            data_filter = filters.streaming_market_data_filter(
                fields=[
                    "EX_BEST_OFFERS",
                    "EX_MARKET_DEF",
                    "EX_TRADED",
                    "EX_TRADED_VOL",
                    "SP_PROJECTED",
                ],
                ladder_levels=3,
            )
            self._stream = self._trading.streaming.create_stream(listener=listener)
            self._stream.subscribe_to_markets(
                market_filter=market_filter,
                market_data_filter=data_filter,
            )

            # Spawn drain + flush threads BEFORE start() blocks
            self._drain_thread = threading.Thread(
                target=self._drain_queue,
                args=(stream_queue,),
                name="fsu1b-drain",
                daemon=True,
            )
            self._drain_thread.start()

            self._flush_thread = threading.Thread(
                target=self._flush_loop,
                name="fsu1b-flush",
                daemon=True,
            )
            self._flush_thread.start()

            self._stream_status = "CONNECTED"
            logger.info("FSU1B stream subscribed; entering consumer loop")
            self._stream.start()  # blocks until stream.stop() is called
            logger.info("FSU1B WebSocket consumer loop exited")
        except Exception as exc:
            logger.exception("FSU1B stream thread failed: %s", exc)
            self._stream_status = "ERROR"

    def _drain_queue(self, q: "Queue") -> None:
        """Pull MarketBook batches off the listener queue, serialise to
        NDJSON, append to the buffer.
        """

        batches = 0
        while not self._stop_flag.is_set():
            try:
                output = q.get(timeout=1)
            except Empty:
                continue
            if not output:
                continue
            batches += 1
            if batches % 100 == 1:
                logger.info(
                    "FSU1B drain batch %d (%d books)", batches, len(output),
                )
            for book in output:
                try:
                    line = market_book_to_ndjson_line(book)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("FSU1B serialise raised: %s", exc)
                    continue
                with self._buffer_lock:
                    self._buffer.append(line)
                # Stats
                self._updates_recorded += 1
                self._last_update_at = _iso_now_ms()
                mid = str(getattr(book, "market_id", "") or "")
                if mid:
                    self._markets_tracked.add(mid)
                md = getattr(book, "market_definition", None)
                if md is not None:
                    venue = getattr(md, "venue", None)
                    if venue:
                        self._venues.add(venue)

    def _flush_loop(self) -> None:
        """Periodic flush — every ``flush_interval_seconds`` OR when
        buffer reaches ``flush_threshold_lines``. Also handles daily
        rollover at 12:00 UTC.
        """

        while not self._stop_flag.is_set():
            # Check threshold-based flush frequently; interval flush slowly
            try:
                self._update_rate()
                self._check_rollover()
                buf_size = len(self._buffer)
                interval_elapsed = self._interval_elapsed()
                if buf_size >= self._settings.flush_threshold_lines or interval_elapsed:
                    self._flush()
            except Exception as exc:  # noqa: BLE001
                logger.warning("flush loop tick raised: %s", exc)
            self._stop_flag.wait(timeout=2)

    def _interval_elapsed(self) -> bool:
        """True when ``flush_interval_seconds`` has passed since last flush."""

        if not self._last_flush_at:
            # Treat recording_start as initial baseline
            baseline = self._recording_start or _iso_now_ms()
        else:
            baseline = self._last_flush_at
        try:
            last = datetime.fromisoformat(baseline.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return True
        elapsed = (datetime.now(tz=timezone.utc) - last).total_seconds()
        return elapsed >= self._settings.flush_interval_seconds

    def _update_rate(self) -> None:
        now = time.time()
        elapsed = now - self._last_minute_check
        if elapsed >= 60:
            delta = self._updates_recorded - self._updates_at_last_minute_check
            self._updates_per_minute = delta * (60.0 / elapsed)
            self._updates_at_last_minute_check = self._updates_recorded
            self._last_minute_check = now

    def _flush(self, force: bool = False) -> None:
        """Drain buffer to local file, then upload local file to GCS.

        On GCS upload failure: retry up to 3 times, then log and keep
        buffering (we don't lose data — the lines are already in the
        local file; next successful flush re-uploads everything).
        """

        with self._buffer_lock:
            if not self._buffer and not force:
                return
            lines = self._buffer
            self._buffer = []

        if lines and self._local_path:
            try:
                with open(self._local_path, "a") as f:
                    for line in lines:
                        f.write(line + "\n")
            except Exception as exc:  # noqa: BLE001
                logger.error("FSU1B local file append failed: %s", exc)
                with self._buffer_lock:
                    self._buffer = lines + self._buffer
                return

        if not self._current_date:
            return

        for attempt in range(3):
            try:
                gcs.upload_ndjson(self._current_date)
                self._last_flush_at = _iso_now_ms()
                self._updates_at_last_flush = self._updates_recorded
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "FSU1B GCS upload attempt %d/3 failed: %s", attempt + 1, exc,
                )
                time.sleep(1)
        logger.error("FSU1B GCS upload exhausted retries; lines stay in local file")

    def _check_rollover(self) -> None:
        """If the trading day has rolled over, flush + write meta for the
        closing day, then start a fresh local file for the new day.
        """

        new_date = gcs.trading_day_for()
        if not self._current_date:
            self._current_date = new_date
            self._local_path = gcs.local_ndjson_path(new_date)
            return
        if new_date == self._current_date:
            return

        logger.info(
            "FSU1B trading day rollover: %s → %s", self._current_date, new_date,
        )
        # Final flush for the closing day
        self._flush(force=True)
        self._write_meta(closed=True)

        # Reset stats for new day
        self._updates_recorded = 0
        self._markets_tracked = set()
        self._venues = set()
        self._updates_at_last_minute_check = 0
        self._updates_at_last_flush = 0

        self._current_date = new_date
        self._local_path = gcs.local_ndjson_path(new_date)
        self._recording_start = _iso_now_ms()
        # Ensure fresh empty local file for the new day
        try:
            open(self._local_path, "w").close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("FSU1B failed to init new local file: %s", exc)

    def _write_meta(self, closed: bool) -> None:
        """Write the meta sidecar for the current trading day."""

        if not self._current_date:
            return
        local_size = 0
        if self._local_path and os.path.exists(self._local_path):
            local_size = os.path.getsize(self._local_path)
        meta = {
            "date": self._current_date,
            "recording_start": self._recording_start,
            "recording_end": _iso_now_ms() if closed else None,
            "markets_recorded": len(self._markets_tracked),
            "total_updates": self._updates_recorded,
            "file_size_bytes": local_size,
            "venues": sorted(self._venues),
        }
        try:
            gcs.write_meta(self._current_date, meta)
            logger.info(
                "FSU1B meta written for %s: %d markets, %d updates, %d bytes",
                self._current_date,
                meta["markets_recorded"],
                meta["total_updates"],
                meta["file_size_bytes"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("FSU1B meta write failed: %s", exc)
