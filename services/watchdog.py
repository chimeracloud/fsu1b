"""
Stream watchdog.

SC go-ahead Phase 2:
  - Check interval:   30s
  - Stale threshold:  60s
  - Action:           force reconnect (cancel current connection)

The watchdog is intentionally separate from the stream loop's own
heartbeat-timeout (2× heartbeat_ms ≈ 10s). The heartbeat-timeout is
the FAST inner detector — it raises as soon as no bytes arrive on the
TCP socket for one heartbeat window. The watchdog is the SLOW outer
safety: if for any reason the stream loop fails to detect a dead
connection, the watchdog catches it at 60s.

The watchdog does NOT log in, does NOT touch credentials, does NOT
read the cache. It only:

  1. Periodically reads `app_state.last_message_at`.
  2. If older than `stream_stale_threshold_s`, calls
     `session.force_disconnect(...)`. The session's supervisor catches
     the cancellation and reconnects with backoff.
"""
from __future__ import annotations

import asyncio
import logging

from core.config import get_settings
from core.state import app_state

logger = logging.getLogger(__name__)


async def run_watchdog(session) -> None:
    """Monitor stream freshness. Forces reconnect when stale.

    On a stale-trigger, publishes `gateway_stream_stale` (Bible §20)
    before calling `session.force_disconnect()`. The supervisor's own
    handler then publishes `gateway_reconnected` once the new TCP
    connection is established.
    """
    # Local import keeps event_publisher off the import graph for tests
    # that don't need it.
    from services.event_publisher import publish

    while True:
        try:
            settings = get_settings()
            await asyncio.sleep(settings.stream_check_interval_s)
        except asyncio.CancelledError:
            return

        if not session.is_running:
            continue

        # Only police a connection that is currently `connected`. While
        # `connecting` / `reconnecting` the inner heartbeat owns the timing.
        if app_state.stream_status != "connected":
            continue

        age = app_state.stream_age_s()
        if age is None:
            # Connected but no message yet — wait for one.
            continue

        stale = settings.stream_stale_threshold_s
        if age > stale:
            logger.warning(
                "Watchdog: stream stale (age=%.1fs > threshold=%ds) — forcing reconnect",
                age, stale,
            )
            try:
                await publish(
                    "gateway_stream_stale",
                    {
                        "age_s": round(age, 1),
                        "threshold_s": stale,
                        "reconnect_count": app_state.reconnect_count,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("stream_stale publish failed: %s", exc)
            session.force_disconnect(
                reason=f"stale age={age:.1f}s threshold={stale}s",
            )
