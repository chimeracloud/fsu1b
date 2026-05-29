"""
Set 3 — CONTENT: stream out + market reads.

Per-sport SSE endpoints (snapshot-per-event semantics) and JSON market
endpoints for cold-start consumers.

Wire contract is documented in the architecture doc §5.

  GET /stream/horse-racing   SSE — event_type_id 7
  GET /stream/football       SSE — event_type_id 1
  GET /stream/tennis         SSE — event_type_id 2
  GET /stream/all            SSE — every event
  GET /stream/snapshot       JSON — current cache (filterable)
  GET /markets               JSON — summary list (filterable)
  GET /markets/{id}          JSON — single market with full runner state

Filtering on /markets and /stream/snapshot:
  ?sport=horse-racing|football|tennis
  ?event_type_id=7|1|2
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.config import EVENT_TYPE_BY_SPORT, SPORT_BY_EVENT_TYPE
from core.state import app_state
from services.market_cache import market_cache

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stream"])


VALID_SPORTS = {"horse-racing", "football", "tennis"}


def _normalise_event_type(
    sport: Optional[str], event_type_id: Optional[str],
) -> Optional[str]:
    """Resolve a filter to a single eventTypeId or None for no filter."""
    if event_type_id is not None:
        return event_type_id
    if sport is not None:
        if sport not in VALID_SPORTS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown sport '{sport}' (expected {sorted(VALID_SPORTS)})",
            )
        return EVENT_TYPE_BY_SPORT[sport]
    return None


# ── SSE per-sport ────────────────────────────────────────────────────────


async def _sse_for_channel(channel: str, request: Request) -> StreamingResponse:
    """Per-sport SSE feed.

    Poll for disconnection every 1s for responsiveness; emit an SSE
    comment-heartbeat every 15s so intermediary proxies don't reap an
    idle connection.
    """
    import time as _t

    queue = await app_state.subscribe(channel)

    async def _gen():
        last_heartbeat = _t.monotonic()
        try:
            # Initial comment proves the wire is up to the consumer.
            yield f": fsu1b sse — channel={channel}\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if _t.monotonic() - last_heartbeat >= 15:
                        yield ": heartbeat\n\n"
                        last_heartbeat = _t.monotonic()
                    continue
                yield f"event: {msg.get('event', 'message')}\n"
                yield f"data: {json.dumps(msg, default=str)}\n\n"
                last_heartbeat = _t.monotonic()
        finally:
            await app_state.unsubscribe(channel, queue)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get("/stream/horse-racing", response_class=StreamingResponse)
async def stream_horse_racing(request: Request) -> StreamingResponse:
    return await _sse_for_channel("horse-racing", request)


@router.get("/stream/football", response_class=StreamingResponse)
async def stream_football(request: Request) -> StreamingResponse:
    return await _sse_for_channel("football", request)


@router.get("/stream/tennis", response_class=StreamingResponse)
async def stream_tennis(request: Request) -> StreamingResponse:
    return await _sse_for_channel("tennis", request)


@router.get("/stream/all", response_class=StreamingResponse)
async def stream_all(request: Request) -> StreamingResponse:
    return await _sse_for_channel("all", request)


# ── Snapshots and reads ──────────────────────────────────────────────────


@router.get("/stream/snapshot")
async def stream_snapshot(
    sport: Optional[str] = Query(default=None),
    event_type_id: Optional[str] = Query(default=None),
) -> dict:
    """Current state of every market in the cache (cold-start helper)."""
    et = _normalise_event_type(sport, event_type_id)
    summaries = await market_cache.all_summaries(event_type_id=et)
    return {
        "source": "fsu1b",
        "source_type": "live",
        "filter": {"sport": sport, "event_type_id": et},
        "count": len(summaries),
        "markets": summaries,
    }


@router.get("/markets")
async def list_markets(
    sport: Optional[str] = Query(default=None),
    event_type_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
) -> dict:
    et = _normalise_event_type(sport, event_type_id)
    summaries = await market_cache.all_summaries(event_type_id=et)
    if status is not None:
        summaries = [m for m in summaries if m.get("status") == status]
    return {
        "count": len(summaries),
        "filter": {"sport": sport, "event_type_id": et, "status": status},
        "markets": summaries,
    }


@router.get("/markets/{market_id}")
async def get_market(market_id: str) -> dict:
    ms = await market_cache.get(market_id)
    if ms is None:
        raise HTTPException(status_code=404, detail=f"market_id {market_id} not found")
    out = ms.to_full()
    out["sport"] = SPORT_BY_EVENT_TYPE.get(ms.event_type_id or "", "unknown")
    return out
