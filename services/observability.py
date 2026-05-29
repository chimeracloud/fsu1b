"""
Standard observability endpoints.

Identical shape across every Chimera FSU per CHI-POL-008 §5.2 so the
Monitoring FSU only ever learns one schema.
"""
from datetime import datetime, timezone
from time import time

from fastapi import APIRouter, Response

from core.version import PHASE, SERVICE_NAME, VERSION

router = APIRouter(tags=["observability"])

_START_TS = time()


@router.get("/health")
def health() -> dict:
    """Liveness — process is up."""
    return {"status": "ok"}


@router.get("/ready")
def ready() -> dict:
    """
    Readiness — can serve real traffic.

    Phase 1: shell-only, no Betfair to attach to → ready.
    Phase 2+: must require both that the LIVE-key stream session is
    delivering messages within `stream_stale_threshold_s` and that the
    DELAYED-key REST session has authenticated successfully.
    """
    return {"ready": True, "phase": PHASE}


@router.get("/info")
def info() -> dict:
    return {
        "service": SERVICE_NAME,
        "version": VERSION,
        "phase": PHASE,
        "description": "Betfair Exchange Gateway (shell)",
    }


@router.get("/metrics")
def metrics() -> Response:
    """Prometheus-format metrics. Phase 1: process uptime only."""
    body = (
        "# HELP fsu1b_uptime_seconds Seconds since the service started.\n"
        "# TYPE fsu1b_uptime_seconds counter\n"
        f"fsu1b_uptime_seconds {time() - _START_TS:.3f}\n"
    )
    return Response(content=body, media_type="text/plain; version=0.0.4")


@router.get("/status")
def status() -> dict:
    return {
        "service": SERVICE_NAME,
        "version": VERSION,
        "phase": PHASE,
        "uptime_s": round(time() - _START_TS, 3),
        "now": datetime.now(timezone.utc).isoformat(),
    }
