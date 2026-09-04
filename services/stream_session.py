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

from datetime import datetime, time as dtime, timedelta, timezone

from core.config import get_settings
from core.state import app_state
from services import stream_client
from services.betfair_auth import keepalive
from services.event_publisher import publish
from services.market_cache import market_cache

logger = logging.getLogger(__name__)

UTC = timezone.utc


async def _publish_safe(event_type: str, payload: dict | None = None) -> None:
    """Publish; swallow errors so the supervisor is never derailed by Pub/Sub."""
    try:
        await publish(event_type, payload or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("event publish failed (%s): %s", event_type, exc)


class StreamSession:
    """Singleton orchestrator for the LIVE-key stream + supporting tasks."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._current_conn: Optional[asyncio.Task] = None
        self._running = False

        # Lifecycle bookkeeping for event publishing (Bible §20).
        # `_was_connected` is True after the first successful TCP+auth
        # cycle of the current session. `_watchdog_drop` is set by
        # `force_disconnect("watchdog…")` so the next successful
        # connect publishes `gateway_reconnected` rather than
        # `gateway_session_recovered`.
        self._was_connected: bool = False
        self._watchdog_drop: bool = False
        self._drop_announced: bool = False  # de-dup gateway_session_dropped

        # True once the CURRENT connection attempt reached 'connected'.
        # Reset before every attempt; the supervisor uses it to decide
        # whether to reset the reconnect backoff. Distinct from
        # `_was_connected`, which latches for the whole session and so
        # cannot tell one attempt from the next.
        self._conn_established: bool = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> dict:
        if self._running:
            return {"accepted": False, "detail": "already running"}

        # Lazy-import to avoid a circular import at module load.
        from services.watchdog import run_watchdog

        self._running = True
        self._was_connected = False
        self._watchdog_drop = False
        self._drop_announced = False
        self._conn_established = False
        app_state.stream_status = "connecting"
        app_state.add_activity("session_start", "starting LIVE stream + workers")

        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._stream_supervisor(), name="bf-supervisor"),
            loop.create_task(self._keepalive_loop(), name="bf-keepalive"),
            loop.create_task(self._maintenance_loop(), name="bf-maintenance"),
            loop.create_task(self._daily_summary_loop(), name="bf-daily-summary"),
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

        # Report the session as stopped, not 'active'. `betfair_auth`
        # last set 'active' at certlogin and nothing since then had
        # reason to revise it, so without this the gateway would claim
        # an active LIVE session while holding no stream at all — the
        # kind of status that gets an incident misdiagnosed.
        #
        # The cached certlogin token is deliberately NOT dropped: a
        # stop/start cycle would then force a fresh certlogin every
        # time, against Betfair's login rate limits. /admin/status
        # reports what is actually held via `token_cached`.
        app_state.live_session.state = "stopped"

        app_state.add_activity("session_stop", "stream stopped")
        return {"accepted": True, "detail": "stopped"}

    def force_disconnect(self, reason: str = "") -> None:
        """Cancel the current TCP connection. Supervisor reconnects with backoff.

        When called by the watchdog, sets `_watchdog_drop=True` so the
        next successful (re)connect publishes `gateway_reconnected`
        instead of `gateway_session_recovered`.
        """
        if self._current_conn is not None and not self._current_conn.done():
            self._current_conn.cancel()
            app_state.add_activity("force_disconnect", reason or "watchdog")
            if "watchdog" in (reason or "").lower() or "stale" in (reason or "").lower():
                self._watchdog_drop = True

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
        """Run connection attempts forever with exponential backoff.

        Event-publishing rules:
          - On a successful connect (stream_status → 'connected') we
            check the prior state and publish exactly one of:
              gateway_reconnected         (watchdog-caused drop)
              gateway_session_recovered   (any other drop)
              (nothing)                   (this is the FIRST connect)
          - When the connection fails / is cancelled (other than
            stop()), we publish gateway_session_dropped exactly once
            per drop window.
        """
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

                # Observe the moment we actually reach 'connected' so we
                # can publish the right event. run_connection sets
                # app_state.stream_status='connected' once the
                # subscription is acknowledged; spawn a watcher task.
                watcher = loop.create_task(
                    self._announce_connected(), name="bf-conn-announce",
                )

                try:
                    await self._current_conn
                finally:
                    if not watcher.done():
                        watcher.cancel()

                if not self._running:
                    return
            except asyncio.CancelledError:
                if not self._running:
                    return
                logger.info("Stream connection cancelled — will reconnect.")
                app_state.add_activity("conn_cancelled", "reconnecting")
                if app_state.stream_status != "disconnected":
                    app_state.stream_status = "reconnecting"
                if self._was_connected and not self._drop_announced:
                    await _publish_safe(
                        "gateway_session_dropped",
                        {"cause": "cancelled", "reconnect_count": app_state.reconnect_count},
                    )
                    self._drop_announced = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Stream connection failed: %r", exc)
                app_state.add_activity("stream_error", repr(exc))
                if app_state.stream_status != "disconnected":
                    app_state.stream_status = "reconnecting"
                if self._was_connected and not self._drop_announced:
                    await _publish_safe(
                        "gateway_session_dropped",
                        {
                            "cause": exc.__class__.__name__,
                            "message": str(exc),
                            "reconnect_count": app_state.reconnect_count,
                        },
                    )
                    self._drop_announced = True
            finally:
                self._current_conn = None

            # Reset the backoff whenever the attempt actually reached
            # 'connected'. Without this the backoff is a function of how
            # many times the session has EVER reconnected rather than of
            # how badly the current reconnect is going: it doubles on
            # every iteration and pins at reconnect_max_backoff_s (300s)
            # after ~9 reconnects, so a drop late in a long session
            # costs five minutes of missed market data where the first
            # drop cost one second.
            #
            # A connection that came up and died before the announcer's
            # 0.5s poll observed it leaves the flag False — which is
            # correct, because that is flapping and should back off.
            if self._conn_established:
                if backoff != 1:
                    logger.info(
                        "Connection was established — resetting backoff %ds -> 1s",
                        backoff,
                    )
                backoff = 1
            self._conn_established = False

            wait = min(backoff, max_b)
            logger.info("Reconnecting in %ds (backoff=%d)", wait, backoff)
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                app_state.stream_status = "disconnected"
                return
            backoff = min(backoff * 2, max_b)

    async def _announce_connected(self) -> None:
        """Wait for stream_status='connected', then publish the right event."""
        # Poll-wait — we're inside the same supervisor task so a small
        # poll interval is fine (it doesn't load Betfair).
        try:
            while self._running:
                if app_state.stream_status == "connected":
                    # This attempt reached 'connected' — the supervisor
                    # reads this to reset the reconnect backoff.
                    self._conn_established = True
                    if not self._was_connected:
                        # First-ever connect for this session — no event.
                        self._was_connected = True
                        self._drop_announced = False
                        return
                    # This is a reconnect — pick the right event type.
                    if self._watchdog_drop:
                        await _publish_safe(
                            "gateway_reconnected",
                            {
                                "trigger": "watchdog",
                                "reconnect_count": app_state.reconnect_count,
                            },
                        )
                        self._watchdog_drop = False
                    else:
                        await _publish_safe(
                            "gateway_session_recovered",
                            {"reconnect_count": app_state.reconnect_count},
                        )
                    self._drop_announced = False
                    return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

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

    async def _daily_summary_loop(self) -> None:
        """Fire `gateway_daily_summary` at the start of each UTC day.

        Sleeps until the next 00:00 UTC, fires the summary for the day
        that just ended, then resets per-day counters.
        """
        while self._running:
            now = datetime.now(UTC)
            tomorrow = (now + timedelta(days=1)).date()
            next_tick = datetime.combine(tomorrow, dtime.min, tzinfo=UTC)
            wait_s = max(60, int((next_tick - now).total_seconds()))
            try:
                await asyncio.sleep(wait_s)
            except asyncio.CancelledError:
                return

            await _publish_safe(
                "gateway_daily_summary",
                {
                    "for_date": (next_tick - timedelta(days=1)).date().isoformat(),
                    "mcm_count": app_state.mcm_count,
                    "mcm_count_by_event_type": dict(app_state.mcm_count_by_event_type),
                    "reconnect_count": app_state.reconnect_count,
                    "markets_in_cache": await market_cache.count(),
                },
            )
            # Reset per-day counters.
            app_state.mcm_count = 0
            app_state.mcm_count_by_event_type.clear()
            app_state.reconnect_count = 0


# Module-level singleton.
stream_session = StreamSession()
