"""
Set 1 — PARAMETERS (admin endpoints).

Identical across every Chimera FSU per CHI-ADR-010. Phase 1 returns
the response *shapes* with placeholder values so:

  • Portal Control FSU can be wired against a stable contract
  • Monitoring FSU learns one schema
  • Phase 2+ replaces internals, not signatures.
"""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from core.version import PHASE, SERVICE_NAME, VERSION
from models.admin import (
    AdminActivityResponse,
    AdminConfigResponse,
    AdminConfigUpdate,
    AdminStatsResponse,
    AdminStatusResponse,
    ControlActionResponse,
    SessionState,
    SubscriptionsSummary,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/status", response_model=AdminStatusResponse)
def status(request: Request) -> AdminStatusResponse:
    """Composite status for the operator dashboard. Phase 1: shell."""
    return AdminStatusResponse(
        service=SERVICE_NAME,
        version=VERSION,
        phase=PHASE,
        live_session=SessionState(state="not_started"),
        delayed_session=SessionState(state="not_started"),
        subscriptions=SubscriptionsSummary(),
        now=datetime.now(timezone.utc),
    )


@router.get("/config", response_model=AdminConfigResponse)
def get_config(request: Request) -> AdminConfigResponse:
    s = request.app.state.settings
    return AdminConfigResponse(
        event_type_ids=list(s.event_type_ids),
        countries=list(s.countries),
        market_types=list(s.market_types),
        stream_check_interval_s=s.stream_check_interval_s,
        stream_stale_threshold_s=s.stream_stale_threshold_s,
        dry_run=s.dry_run,
    )


@router.put("/config", response_model=AdminConfigResponse)
def put_config(update: AdminConfigUpdate, request: Request) -> AdminConfigResponse:
    """
    Phase 1: accepts the shape, returns current values.

    Phase 4 wires this to GCS-backed config per CHI-POL-006 (portal-
    editable config). No env vars for settings. Ever.
    """
    return get_config(request)


@router.get("/stats", response_model=AdminStatsResponse)
def stats() -> AdminStatsResponse:
    """Phase 1: all zeroes — counters wire up in Phase 2 and 3."""
    return AdminStatsResponse(
        messages_per_s=0.0,
        bytes_in_per_s=0.0,
        bytes_out_per_s=0.0,
        orders_placed=0,
        orders_cancelled=0,
        rest_errors=0,
        stream_reconnects=0,
    )


@router.get("/activity", response_model=AdminActivityResponse)
def activity() -> AdminActivityResponse:
    """Phase 1: empty — activity feed wires up alongside the stream."""
    return AdminActivityResponse(events=[])


@router.post("/control/{action}", response_model=ControlActionResponse)
def control(
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
    """Phase 1: accepts verbs, records intent, does not act."""
    return ControlActionResponse(
        action=action,
        accepted=True,
        executed=False,
        note="Phase 1 shell — control verbs accepted but not yet wired.",
        at=datetime.now(timezone.utc),
    )


@router.get("/events")
async def events() -> StreamingResponse:
    """
    SSE — internal control events for the operator dashboard.

    Phase 1: opens the connection, sends one comment heartbeat so the
    portal can prove its SSE wiring works, then closes. Phase 2+
    streams real session / control / error events.
    """

    async def _gen():
        yield ": fsu1b admin event stream — phase 1 shell\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")
