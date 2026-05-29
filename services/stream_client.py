"""
Betfair Exchange Stream API client — async TLS.

Ported from FSU1A's `stream_client.py`. Adapted for FSU1B:

  * Multi-eventTypeId subscription from day one (SC go-ahead: 7, 1, 2).
  * Per-sport SSE broadcast — every MarketChange is routed to its
    sport channel based on marketDefinition.eventTypeId via
    SPORT_BY_EVENT_TYPE.
  * Watchdog-driven external reconnect — see services.watchdog. The
    stream loop's own heartbeat-timeout (2× heartbeat_ms) is the inner
    fast-path; the watchdog (60s stale = reconnect) is the outer safety.
  * Module-level singleton orchestration via services.stream_session.

Protocol (per Betfair ESA spec):
  1. Plain TLS to stream-api.betfair.com:443 (no client cert; cert
     is only for certlogin).
  2. Server sends op=connection → connectionId.
  3. Client sends op=authentication (appKey + sessionToken).
  4. Server sends op=status (statusCode SUCCESS).
  5. Client sends op=marketSubscription (initialClk/clk if resuming).
  6. Server sends op=status acknowledging the subscription.
  7. Read loop: op=mcm with mc[] deltas → market_cache.apply_mcm().
  8. On error or heartbeat timeout → raise; orchestrator reconnects
     with exponential backoff.

clk / initialClk resumption:
  Stored at module level so they survive reconnects within one
  process. Updated only on non-segmented messages or SEG_END
  (segments must be applied immediately but the cursor only advances
  on SEG_END).

Stream status 503 inside an MCM:
  High-latency indicator — do NOT disconnect, just flag in state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from core.config import SPORT_BY_EVENT_TYPE, get_settings
from core.secrets import get_credentials
from core.state import app_state
from services.betfair_auth import get_session_token, refresh_session
from services.market_cache import market_cache

logger = logging.getLogger(__name__)
UTC = timezone.utc

# Resubscription cursors — module-level so they survive reconnects.
_initial_clk: Optional[str] = None
_clk: Optional[str] = None

# Outgoing message ID — monotonic.
_msg_id = 0


def _next_id() -> int:
    global _msg_id
    _msg_id += 1
    return _msg_id


def reset_cursors_for_test() -> None:
    global _initial_clk, _clk, _msg_id
    _initial_clk = None
    _clk = None
    _msg_id = 0


def cursors() -> dict[str, Optional[str]]:
    """Inspect current clk / initialClk for /admin/status."""
    return {"initial_clk": _initial_clk, "clk": _clk}


# ─────────────────────────────────────────────────────────────────────────────
# Public — driven by services.stream_session
# ─────────────────────────────────────────────────────────────────────────────


async def run_connection() -> None:
    """One TLS connection: connect → auth → subscribe → read until dead.

    Raises on any failure so the orchestrator can reconnect with
    backoff. Returns normally only on a clean shutdown signalled via
    cancellation.
    """
    global _initial_clk, _clk

    settings = get_settings()
    ssl_ctx = ssl.create_default_context()

    # Default asyncio limit is 64KB — Betfair SUB_IMAGE responses can be
    # tens of MB on a busy day. 10MB is safe.
    reader, writer = await asyncio.open_connection(
        settings.stream_host,
        settings.stream_port,
        ssl=ssl_ctx,
        limit=10 * 1024 * 1024,
    )

    app_state.add_activity("stream_connecting", f"{settings.stream_host}:{settings.stream_port}")

    try:
        # ── 1. server connection message ──
        conn_msg = await _recv(reader)
        if conn_msg.get("op") != "connection":
            raise RuntimeError(f"expected op=connection, got {conn_msg.get('op')}")
        app_state.connection_id = conn_msg.get("connectionId")
        logger.info("ESA connected — connectionId=%s", app_state.connection_id)

        # ── 2. authenticate ──
        try:
            token = await get_session_token(key="live")
        except Exception:
            token = await refresh_session(key="live")

        creds = get_credentials("live")
        await _send(
            writer,
            {
                "op": "authentication",
                "id": _next_id(),
                "appKey": creds["app_key"],
                "session": token,
            },
        )
        auth_resp = await _recv(reader)
        _check_status(auth_resp, "authentication")

        # ── 3. subscribe ──
        sub: dict = {
            "op": "marketSubscription",
            "id": _next_id(),
            "marketFilter": _build_market_filter(settings),
            "marketDataFilter": {"fields": list(settings.market_data_fields)},
        }
        if _initial_clk:
            sub["initialClk"] = _initial_clk
        if _clk:
            sub["clk"] = _clk

        await _send(writer, sub)
        sub_resp = await _recv(reader)
        _check_status(sub_resp, "marketSubscription")

        app_state.stream_status = "connected"
        app_state.add_activity(
            "stream_subscribed",
            f"event_type_ids={list(settings.event_type_ids)} "
            f"countries={list(settings.countries)}",
        )
        logger.info("Market subscription active.")

        # ── 4. read loop ──
        heartbeat_timeout = (settings.heartbeat_ms * 2) / 1000.0

        async for msg in _message_stream(reader, heartbeat_timeout):
            op = msg.get("op")
            if op == "mcm":
                await _handle_mcm(msg)
            elif op == "status":
                _handle_status_msg(msg)
            elif op == "connection":
                app_state.connection_id = msg.get("connectionId")

    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


# ─────────────────────────────────────────────────────────────────────────────
# MCM + per-sport routing
# ─────────────────────────────────────────────────────────────────────────────


async def _handle_mcm(msg: dict) -> None:
    """Apply mcm to the cache, route per-sport snapshots to SSE subscribers."""
    global _initial_clk, _clk

    # 503 = high latency, do NOT disconnect.
    stream_status = msg.get("status")
    if stream_status == 503:
        if not app_state.stream_latency_503:
            app_state.stream_latency_503 = True
            logger.warning("Stream status 503 — high latency, continuing")
            app_state.add_activity("stream_latency", "status=503")
    else:
        app_state.stream_latency_503 = False

    # Apply deltas; collect touched markets.
    touched = await market_cache.apply_mcm(msg) if msg.get("mc") else []

    # Update segmentation cursor only on non-segmented / SEG_END.
    seg = msg.get("segmentType")
    if seg is None or seg == "SEG_END":
        if "initialClk" in msg:
            _initial_clk = msg["initialClk"]
        if "clk" in msg:
            _clk = msg["clk"]

    # Tag last-message timestamp and per-event-type counters.
    # Use the first touched market's eventTypeId as the representative key
    # for the *connection-level* counter; per-market broadcast still routes
    # individually below.
    representative_event_type = None
    if touched:
        representative_event_type = touched[0].event_type_id
    app_state.note_message(representative_event_type)

    # Per-market broadcast: snapshot-per-event semantics.
    for ms in touched:
        et = ms.event_type_id
        sport = SPORT_BY_EVENT_TYPE.get(et or "", "unknown")
        event = {
            "event": "market_change",
            "ts": datetime.now(UTC).isoformat(),
            "source": "fsu1b",
            "source_type": "live",
            "sport": sport,
            "event_type_id": et,
            "change_type": msg.get("ct"),  # SUB_IMAGE | RESUB_DELTA | HEARTBEAT | None
            "market": ms.to_summary(),
            "runners": [r.to_dict() for r in ms.runners.values()],
        }
        await app_state.broadcast(sport, event)


def _handle_status_msg(msg: dict) -> None:
    err = msg.get("errorCode")
    if not err:
        return
    logger.error("Stream status error: %s — %s", err, msg.get("errorMessage"))
    app_state.add_activity("stream_status_error", str(msg))
    # Auth-class errors must force a fresh certlogin on next reconnect.
    if err in ("INVALID_SESSION_INFORMATION", "MAX_CONNECTION_LIMIT_EXCEEDED", "NOT_AUTHORIZED"):
        _stores_drop_token()
        raise RuntimeError(f"stream auth error: {err}")


def _stores_drop_token() -> None:
    # Imported here to avoid circular dependency at module load.
    from services.betfair_auth import _stores
    _stores["live"].token = None
    _stores["live"].acquired_at = None


# ─────────────────────────────────────────────────────────────────────────────
# Low-level I/O
# ─────────────────────────────────────────────────────────────────────────────


async def _send(writer: asyncio.StreamWriter, msg: dict) -> None:
    writer.write((json.dumps(msg) + "\r\n").encode())
    await writer.drain()


async def _recv(reader: asyncio.StreamReader) -> dict:
    line = await reader.readline()
    if not line:
        raise RuntimeError("stream EOF on recv")
    return json.loads(line.decode().strip())


async def _message_stream(
    reader: asyncio.StreamReader, timeout: float
) -> AsyncIterator[dict]:
    """Yield parsed JSON messages; raise on read timeout (dead connection)."""
    while True:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"no message in {timeout:.1f}s — heartbeat dead") from exc
        if not line:
            raise RuntimeError("stream EOF")
        text = line.decode().strip()
        if not text:
            continue
        try:
            yield json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("malformed stream message: %s — raw=%r", exc, text[:200])


def _check_status(msg: dict, context: str) -> None:
    if msg.get("statusCode") != "SUCCESS":
        raise RuntimeError(
            f"{context} failed: statusCode={msg.get('statusCode')} "
            f"error={msg.get('errorCode')} msg={msg.get('errorMessage')}"
        )


def _build_market_filter(settings) -> dict:
    f: dict = {}
    if settings.event_type_ids:
        f["eventTypeIds"] = list(settings.event_type_ids)
    if settings.countries:
        f["countryCodes"] = list(settings.countries)
    if settings.market_types:
        f["marketTypes"] = list(settings.market_types)
    return f
