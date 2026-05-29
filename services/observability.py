"""
Standard observability endpoints.

Identical shape across every Chimera FSU per CHI-POL-008 §5.2.

Phase 2: `/ready` is now gated on actual stream freshness.
"""
from datetime import datetime, timezone
from time import time
from typing import Any

from fastapi import APIRouter, Response

from core.config import get_settings
from core.state import app_state
from core.version import PHASE, SERVICE_NAME, VERSION

router = APIRouter(tags=["observability"])

_START_TS = time()


@router.get("/health")
def health() -> dict[str, Any]:
    """Liveness — process is up."""
    return {"status": "ok"}


@router.get("/ready")
def ready(response: Response) -> dict[str, Any]:
    """
    Readiness — can serve real traffic.

    Phase 2 gates on stream freshness:
      ready = session is running AND stream is connected AND
              a message arrived within `stream_stale_threshold_s`.

    When `auto_start=False` and no operator has started the gateway,
    /ready returns 200 with `mode='idle'` so Cloud Run's healthcheck
    doesn't bounce a deliberately idle container.
    """
    settings = get_settings()
    sess = app_state
    session_state = (
        "running" if hasattr(app_state, "_running_marker")  # placeholder
        else None
    )
    # Use the real session singleton for is_running, not a placeholder.
    from services.stream_session import stream_session

    if not stream_session.is_running:
        # Deliberately idle container — still ready for admin traffic.
        return {
            "ready": True,
            "phase": PHASE,
            "mode": "idle",
            "stream_status": sess.stream_status,
            "note": "session not started — POST /admin/control/start to connect",
        }

    fresh = sess.stream_is_fresh(settings.stream_stale_threshold_s)
    if sess.stream_status == "connected" and fresh:
        return {
            "ready": True,
            "phase": PHASE,
            "mode": "running",
            "stream_status": "connected",
            "stream_age_s": sess.stream_age_s(),
        }

    # Session running but stream not fresh → not ready.
    response.status_code = 503
    return {
        "ready": False,
        "phase": PHASE,
        "mode": "running",
        "stream_status": sess.stream_status,
        "stream_age_s": sess.stream_age_s(),
        "stale_threshold_s": settings.stream_stale_threshold_s,
    }


@router.get("/info")
def info() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "version": VERSION,
        "phase": PHASE,
        "description": "Betfair Exchange Gateway",
    }


@router.get("/metrics")
def metrics() -> Response:
    """Prometheus-format metrics."""
    sess = app_state
    body_lines = [
        "# HELP fsu1b_uptime_seconds Seconds since the service started.",
        "# TYPE fsu1b_uptime_seconds counter",
        f"fsu1b_uptime_seconds {time() - _START_TS:.3f}",
        "# HELP fsu1b_mcm_total Total Betfair MarketChangeMessages received.",
        "# TYPE fsu1b_mcm_total counter",
        f"fsu1b_mcm_total {sess.mcm_count}",
        "# HELP fsu1b_reconnects_total Total stream reconnections.",
        "# TYPE fsu1b_reconnects_total counter",
        f"fsu1b_reconnects_total {sess.reconnect_count}",
        "# HELP fsu1b_stream_messages_per_s Recent message rate (60s window).",
        "# TYPE fsu1b_stream_messages_per_s gauge",
        f"fsu1b_stream_messages_per_s {sess.messages_per_s_recent():.3f}",
    ]
    age = sess.stream_age_s()
    if age is not None:
        body_lines += [
            "# HELP fsu1b_stream_age_seconds Seconds since the last MCM.",
            "# TYPE fsu1b_stream_age_seconds gauge",
            f"fsu1b_stream_age_seconds {age:.3f}",
        ]
    body = "\n".join(body_lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4")


@router.get("/status")
def status() -> dict[str, Any]:
    sess = app_state
    return {
        "service": SERVICE_NAME,
        "version": VERSION,
        "phase": PHASE,
        "uptime_s": round(time() - _START_TS, 3),
        "now": datetime.now(timezone.utc).isoformat(),
        "stream_status": sess.stream_status,
        "stream_age_s": sess.stream_age_s(),
        "mcm_count": sess.mcm_count,
        "reconnect_count": sess.reconnect_count,
    }
