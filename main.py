"""
FSU1B — Betfair Exchange Gateway.

Phase 2 — LIVE-key async TLS stream + per-sport SSE + watchdog.
Set 1 (admin) endpoints reflect real session/stream state. DELAYED-key
REST surface lands in Phase 3.

References:
  - CHI-POL-005  FSU Build Workflow
  - CHI-POL-008  Shell-First Build Policy
  - CHI-ADR-010  Three Endpoint Sets
  - CHI-ADR-013  One task, one job
  - CHI-ADR-015 .. 023  FSU1B-specific decisions (Bible)
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import get_settings
from core.logging import configure_logging
from core.version import SERVICE_DESCRIPTION, SERVICE_NAME, VERSION
from services import admin, observability, rest_routes, stream_routes
from services.stream_session import stream_session

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    app.state.settings = settings
    app.state.stream_session = stream_session

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

app.include_router(observability.router)
app.include_router(admin.router)
app.include_router(stream_routes.router)
app.include_router(rest_routes.router)
