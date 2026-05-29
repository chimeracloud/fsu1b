"""
Stream session orchestrator.

Owns the stream supervisor task, the watchdog task, the keepalive
task, and the maintenance task. Exposes start() / stop() / status() /
force_disconnect() to the rest of the application.

Reconnect strategy:

  * The inner stream loop (services.stream_client.run_connection)
    handles ONE connection. On any error it raises.

  * The supervisor wraps each run_connection() call in its own asyncio
    Task (`self._current_conn`) and awaits it. Catching the raise, it
    backs off (exponential, capped by reconnect_max_backoff_s) and
    starts a new run_connection().

  * The watchdog (services.watchdog) calls `force_disconnect()` when
    the stream is stale (no message for stream_stale_threshold_s).
    `force_disconnect()` cancels the inner conn task; the supervisor
    falls back into the same backoff path. The supervisor stays alive.

This keeps the watchdog decoupled from the supervisor's internal
state — it just asks the session to disconnect, and the session
handles the reconnect.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from core.config import get_settings
from core.state import app_state
from services import stream_client
from services.betfair_auth import keepalive
from services.market_cache import market_cache

logger = logging.getLogger(__name__)


class StreamSession:
    """Singleton orchestrator for the LIVE-key stream + supporting tasks."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._current_conn: Optional[asyncio.Task] = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> dict:
        if self._running:
            return {"accepted": False, "detail": "already running"}

        # Lazy-import to avoid a circular import at module load.
        from services.watchdog import run_watchdog

        self._running = True
        app_state.stream_status = "connecting"
        app_state.add_activity("session_start", "starting LIVE stream + workers")

        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._stream_supervisor(), name="bf-supervisor"),
            loop.create_task(self._keepalive_loop(), name="bf-keepalive"),
            loop.create_task(self._maintenance_loop(), name="bf-maintenance"),
            loop.create_task(run_watchdog(self), name="bf-watchdog"),
        ]
        return {"accepted": True, "detail": "started"}

    async def stop(self) -> dict:
        if not self._running:
            return {"accepted": False, "detail": "not running"}

        self._running = False
        if self._current_conn is not None and not self._current_conn.done():
            self._current_conn.cancel()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._current_conn = None
        app_state.stream_status = "disconnected"
        app_state.add_activity("session_stop", "stream stopped")
        return {"accepted": True, "detail": "stopped"}

    def force_disconnect(self, reason: str = "") -> None:
        """Cancel the current TCP connection. Supervisor reconnects with backoff."""
        if self._current_conn is not None and not self._current_conn.done():
            self._current_conn.cancel()
            app_state.add_activity("force_disconnect", reason or "watchdog")

    def status(self) -> dict:
        settings = get_settings()
        return {
            "running": self._running,
            "stream_status": app_state.stream_status,
            "connection_id": app_state.connection_id,
            "stream_latency_503": app_state.stream_latency_503,
            "last_message_at": (
                app_state.last_message_at.isoformat()
                if app_state.last_message_at else None
            ),
            "stream_age_s": app_state.stream_age_s(),
            "reconnect_count": app_state.reconnect_count,
            "messages_per_s_recent": round(app_state.messages_per_s_recent(), 2),
            "mcm_count": app_state.mcm_count,
            "mcm_count_by_event_type": dict(app_state.mcm_count_by_event_type),
            "cursors": stream_client.cursors(),
            "watchdog_stale_threshold_s": settings.stream_stale_threshold_s,
            "watchdog_check_interval_s": settings.stream_check_interval_s,
        }

    # ── Background loops ─────────────────────────────────────────────────

    async def _stream_supervisor(self) -> None:
        """Run connection attempts forever with exponential backoff."""
        backoff = 1
        first = True

        while self._running:
            if not first:
                app_state.reconnect_count += 1
            first = False

            app_state.stream_status = "connecting"
            settings = get_settings()
            max_b = settings.reconnect_max_backoff_s

            try:
                loop = asyncio.get_running_loop()
                self._current_conn = loop.create_task(
                    stream_client.run_connection(), name="bf-conn",
                )
                await self._current_conn
                # run_connection returning cleanly = shutdown signalled.
                if not self._running:
                    return
            except asyncio.CancelledError:
                # Either stop() was called or watchdog cancelled the conn.
                if not self._running:
                    return
                logger.info("Stream connection cancelled — will reconnect.")
                app_state.add_activity("conn_cancelled", "reconnecting")
                if app_state.stream_status != "disconnected":
                    app_state.stream_status = "reconnecting"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Stream connection failed: %r", exc)
                app_state.add_activity("stream_error", repr(exc))
                if app_state.stream_status != "disconnected":
                    app_state.stream_status = "reconnecting"
            finally:
                self._current_conn = None

            wait = min(backoff, max_b)
            logger.info("Reconnecting in %ds (backoff=%d)", wait, backoff)
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                app_state.stream_status = "disconnected"
                return
            backoff = min(backoff * 2, max_b)

    async def _keepalive_loop(self) -> None:
        """Keep both LIVE and DELAYED sessions warm.

        DELAYED is only kept alive if it has been logged in at least
        once (i.e. someone called a REST endpoint). Until then, the
        delayed loop simply waits.
        """
        while self._running:
            settings = get_settings()
            interval = max(60, settings.session_keepalive_hours * 3600)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                ok = await keepalive(key="live")
                logger.info("LIVE keepalive: %s", "ok" if ok else "failed")
            except Exception as exc:  # noqa: BLE001
                logger.warning("LIVE keepalive raised: %s", exc)
            if app_state.delayed_session.last_login is not None:
                try:
                    ok = await keepalive(key="delayed")
                    logger.info("DELAYED keepalive: %s", "ok" if ok else "failed")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("DELAYED keepalive raised: %s", exc)

    async def _maintenance_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                return
            try:
                removed = await market_cache.remove_closed()
                if removed:
                    logger.info("Evicted %d CLOSED markets", removed)
                    app_state.add_activity(
                        "markets_evicted", f"{removed} CLOSED markets"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("maintenance raised: %s", exc)


# Module-level singleton.
stream_session = StreamSession()
