"""
FSU1B — Betfair Exchange Gateway.

Phase 4 — integration: GCS-backed config, Source Manifest registration,
Pub/Sub event envelope publishing, portal proxy IAM glue.

References:
  - CHI-POL-005  FSU Build Workflow
  - CHI-POL-006  Portal as Single Auth Boundary
  - CHI-POL-008  Shell-First Build Policy
  - CHI-ADR-010  Three Endpoint Sets
  - CHI-ADR-013  One task, one job
  - CHI-ADR-014  Portal Proxy Pattern
  - CHI-ADR-015 .. 023  FSU1B-specific decisions (Bible)
  - Bible Section 20  Event Architecture (envelope + courier)
  - Bible Section 21  Plugin Source Declaration & Source Manifest
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from core.config import get_settings, replace_settings
from core.gcs_config import load_config_from_gcs
from core.logging import configure_logging
from core.state import app_state
from core.version import SERVICE_DESCRIPTION, SERVICE_NAME, VERSION
from services import admin, observability, rest_routes, stream_routes
from services.event_publisher import publish
from services.source_manifest import register_best_effort
from services.stream_session import stream_session

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()

    # Phase 4: hydrate config from GCS. Falls back to in-memory defaults
    # if the bucket is unreachable so the admin surface still serves.
    load_config_from_gcs()

    # If the deploy injected $SERVICE_URL, propagate it into Settings
    # so the Source Manifest entry advertises the right URL. Env vars
    # are NEVER used for settings (CHI-POL-006) — this is purely a
    # deploy-time identity hand-off and not a tunable knob.
    if "SERVICE_URL" in os.environ and os.environ["SERVICE_URL"]:
        replace_settings(service_url=os.environ["SERVICE_URL"])

    settings = get_settings()
    app.state.settings = settings
    app.state.stream_session = stream_session

    # Source Manifest registration (Bible §21). Best-effort.
    register_best_effort()

    # Infrastructure event: we're up.
    await publish("gateway_started", {
        "version": VERSION,
        "phase": 4,
        "auto_start": settings.auto_start,
        "service_url": settings.service_url,
    })

    if settings.auto_start:
        logger.info("auto_start=True — starting LIVE stream in background")
        asyncio.create_task(stream_session.start(), name="bf-autostart")
    else:
        logger.info(
            "auto_start=False — gateway idle. POST /admin/control/start to begin."
        )

    try:
        yield
    finally:
        # Best-effort shutdown event then stop the stream.
        try:
            await publish("gateway_stopped", {"version": VERSION})
        except Exception:  # noqa: BLE001
            pass
        try:
            await stream_session.stop()
        except Exception:  # noqa: BLE001
            pass
        logger.info("FSU1B shut down.")


app = FastAPI(
    title=SERVICE_NAME,
    description=SERVICE_DESCRIPTION,
    version=VERSION,
    docs_url="/admin/docs",
    redoc_url=None,
    lifespan=lifespan,
)

@app.middleware("http")
async def _record_endpoint_call(request: Request, call_next):
    """Stamp every inbound request so DATA OUT LEDs in the portal reflect
    real consumer activity (not a placeholder).

    Excludes liveness / observability paths so health-pollers don't drown
    out actual REST traffic in the per-endpoint feed.
    """
    path = request.url.path
    if path not in {"/health", "/ready", "/metrics", "/info", "/status"}:
        try:
            app_state.note_endpoint_call(path)
        except Exception:  # noqa: BLE001 — middleware must never break the request
            pass
    return await call_next(request)


app.include_router(observability.router)
app.include_router(admin.router)
app.include_router(stream_routes.router)
app.include_router(rest_routes.router)
