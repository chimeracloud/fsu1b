"""
Set 1 — PARAMETERS (admin endpoints).

Identical across every Chimera FSU per CHI-ADR-010. Phase 2 reflects
real LIVE-session/stream state; DELAYED session is still
`not_started` until Phase 3 wires the REST surface.

Control verbs supported in Phase 2:
  start              — start the LIVE stream + workers (+ watchdog + keepalive)
  stop               — stop the LIVE stream + workers (DELAYED session left alone)
  reconnect_stream   — force-disconnect; supervisor reconnects with backoff
  pause / resume     — accepted but not yet wired (Phase 3 placeholder)
  relogin_rest       — accepted but not yet wired (Phase 3)
  test               — sanity poke; verifies admin surface
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from core.config import (
    SPORT_BY_EVENT_TYPE,
    get_settings,
    replace_settings,
)
from core.state import app_state
from core.version import PHASE, SERVICE_NAME, VERSION
from models.admin import (
    ActivityEvent,
    AdminActivityResponse,
    AdminConfigResponse,
    AdminConfigUpdate,
    AdminStatsResponse,
    AdminStatusResponse,
    ControlActionResponse,
    SessionState,
    SubscriptionsSummary,
)
from services.market_cache import market_cache
from services.stream_session import stream_session

router = APIRouter(prefix="/admin", tags=["admin"])


def _session_state(info, *, include_clk: bool) -> SessionState:
    from services import stream_client

    return SessionState(
        state=info.state,
        last_login=info.last_login,
        last_keepalive=info.last_keepalive,
        last_clk=(stream_client.cursors()["clk"] if include_clk else None),
        last_error=info.last_error,
    )


async def _subscriptions_summary() -> SubscriptionsSummary:
    counts = await market_cache.count_by_event_type()
    total = sum(counts.values())
    by_sport = {
        SPORT_BY_EVENT_TYPE.get(et, et): n for et, n in counts.items()
    }
    return SubscriptionsSummary(market_count=total, by_sport=by_sport)


@router.get("/status", response_model=AdminStatusResponse)
async def status(request: Request) -> AdminStatusResponse:
    return AdminStatusResponse(
        service=SERVICE_NAME,
        version=VERSION,
        phase=PHASE,
        live_session=_session_state(app_state.live_session, include_clk=True),
        delayed_session=_session_state(app_state.delayed_session, include_clk=False),
        subscriptions=await _subscriptions_summary(),
        stream=stream_session.status(),
        now=datetime.now(timezone.utc),
    )


@router.get("/config", response_model=AdminConfigResponse)
def get_config() -> AdminConfigResponse:
    s = get_settings()
    return AdminConfigResponse(
        event_type_ids=list(s.event_type_ids),
        countries=list(s.countries),
        market_types=list(s.market_types),
        stream_check_interval_s=s.stream_check_interval_s,
        stream_stale_threshold_s=s.stream_stale_threshold_s,
        dry_run=s.dry_run,
        auto_start=s.auto_start,
    )


@router.put("/config", response_model=AdminConfigResponse)
def put_config(update: AdminConfigUpdate) -> AdminConfigResponse:
    """
    Update settings (in-memory). Phase 4 wires GCS persistence per
    CHI-POL-006.

    Subscription-filter changes apply on the NEXT stream connection;
    a live subscription cannot be edited mid-flight without
    resubscribing.
    """
    changes = {k: v for k, v in update.model_dump(exclude_unset=True).items() if v is not None}
    if changes:
        replace_settings(**changes)
    return get_config()


@router.get("/stats", response_model=AdminStatsResponse)
async def stats() -> AdminStatsResponse:
    return AdminStatsResponse(
        messages_per_s=round(app_state.messages_per_s_recent(), 3),
        mcm_count=app_state.mcm_count,
        mcm_count_by_event_type=dict(app_state.mcm_count_by_event_type),
        reconnect_count=app_state.reconnect_count,
        markets_subscribed=await market_cache.count(),
        subscribers_by_channel=app_state.subscriber_count(),
    )


@router.get("/activity", response_model=AdminActivityResponse)
def activity() -> AdminActivityResponse:
    events = [ActivityEvent(**e) for e in app_state.recent_activity(limit=100)]
    return AdminActivityResponse(events=events)


@router.post("/control/{action}", response_model=ControlActionResponse)
async def control(
    action: Literal[
        "start",
        "stop",
        "pause",
        "resume",
        "reconnect_stream",
        "relogin_rest",
        "test",
    ],
) -> ControlActionResponse:
    now = datetime.now(timezone.utc)

    if action == "start":
        result = await stream_session.start()
        return ControlActionResponse(
            action=action,
            accepted=bool(result.get("accepted")),
            executed=bool(result.get("accepted")),
            note=result.get("detail", ""),
            at=now,
        )

    if action == "stop":
        result = await stream_session.stop()
        return ControlActionResponse(
            action=action,
            accepted=True,
            executed=bool(result.get("accepted")),
            note=result.get("detail", ""),
            at=now,
        )

    if action == "reconnect_stream":
        if not stream_session.is_running:
            raise HTTPException(status_code=409, detail="stream not running")
        stream_session.force_disconnect(reason="manual reconnect_stream")
        return ControlActionResponse(
            action=action,
            accepted=True,
            executed=True,
            note="forced disconnect; supervisor will reconnect",
            at=now,
        )

    if action in {"pause", "resume", "relogin_rest"}:
        return ControlActionResponse(
            action=action,
            accepted=True,
            executed=False,
            note=f"{action} — accepted; wires up in Phase 3",
            at=now,
        )

    # test
    return ControlActionResponse(
        action=action,
        accepted=True,
        executed=True,
        note="ok",
        at=now,
    )


@router.get("/events")
async def events_sse():
    """
    Admin SSE feed — yields a single comment so the portal can verify the
    wiring. Phase 4 streams real control / session events for the
    operator dashboard.
    """
    from fastapi.responses import StreamingResponse

    async def _gen():
        yield ": fsu1b admin event stream — phase 2\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")
