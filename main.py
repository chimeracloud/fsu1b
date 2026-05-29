"""
FSU1B — Betfair Exchange Gateway.

Phase 1 — shell only. No Betfair connection. Returns shapes and verbs
so downstream contracts (Portal Control FSU, Live Betting Control FSU,
recorders, engines) can be wired against a stable surface while
Phase 2+ pour in content.

References:
  - CHI-POL-005  FSU Build Workflow
  - CHI-POL-008  Shell-First Build Policy
  - CHI-ADR-010  Three Endpoint Sets
  - CHI-ADR-013  One task, one job
  - CHI-ADR-015 .. 023  FSU1B-specific decisions (Bible)
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import get_settings
from core.logging import configure_logging
from core.version import SERVICE_DESCRIPTION, SERVICE_NAME, VERSION
from services import admin, observability


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    app.state.settings = get_settings()
    # Phase 1: no Betfair sessions yet.
    yield
    # Phase 1: nothing to tear down.


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
