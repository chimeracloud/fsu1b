"""Betfair Stream Recorder — FastAPI entrypoint (SHELL).

Phase 1 of the recorder build: all endpoints exist and return
plausible mock responses so we can verify the deploy + auth path
before wiring the actual recording logic.

Phase 2 will replace each mock response with real data from the
recorder module (recorder.py).

Architectural rules baked in from the brief:

* This service is a TAPE RECORDER. It records, it does not
  calculate. No P&L, no aggregation, no derived values.
* It runs independently of CLE V2. CLE V2 crashing or redeploying
  has no effect on this service.
* Daily file rolls at 12:00 UTC. New file = new day.
* On restart mid-day, append to the same daily file.

Phase 1 mock responses are clearly labelled so the operator sees
"shell live" rather than thinking recording has started.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from settings import RecorderSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ──────────────────────────────────────────────────────────────────────────────
# Lifespan — in Phase 1 this just holds the settings object. Phase 2 spins
# up the recorder thread.
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = RecorderSettings()
    app.state.shell_mode = True  # Phase 1 flag — remove in Phase 2
    app.state.started_at = _iso_now()
    logger.info("betfair-recorder shell live (Phase 1 — no recording yet)")
    try:
        yield
    finally:
        logger.info("betfair-recorder shut down")


app = FastAPI(
    title="Chimera Betfair Stream Recorder",
    description=(
        "Records raw Betfair MarketBook data as NDJSON for replay and "
        "audit. Pure capture — no calculations, no P&L, no aggregation."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://chimerasportstrading.com",
        "https://www.chimerasportstrading.com",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────────────
# Admin
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/admin/status", tags=["admin"])
async def get_status() -> dict[str, Any]:
    """Recorder state.

    Phase 1 mock: recording=False, no file yet. Phase 2 will return
    live counts + current file path + updates/min.
    """

    return {
        "service": "betfair-recorder",
        "version": "0.1.0",
        "shell_mode": app.state.shell_mode,
        "recording": False,
        "stream_status": "DISCONNECTED",
        "current_file": None,
        "markets_tracked": 0,
        "updates_recorded": 0,
        "file_size_bytes": 0,
        "recording_since": None,
        "started_at": app.state.started_at,
        "timestamp": _iso_now(),
    }


@app.get("/admin/config", tags=["admin"])
async def get_config() -> dict[str, Any]:
    return app.state.settings.to_dict()


@app.put("/admin/config", tags=["admin"])
async def put_config(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Replace recorder settings. In Phase 2 this persists to GCS."""

    try:
        new_settings = RecorderSettings.from_dict(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid settings: {exc}") from exc
    app.state.settings = new_settings
    return {"settings": new_settings.to_dict(), "persisted": False, "shell_mode": True}


@app.get("/admin/stats", tags=["admin"])
async def get_stats() -> dict[str, Any]:
    """Recorder activity stats. Phase 1 mock returns zeros."""

    return {
        "shell_mode": app.state.shell_mode,
        "updates_per_minute": 0,
        "file_size_bytes": 0,
        "markets_count": 0,
        "last_update_at": None,
        "timestamp": _iso_now(),
    }


@app.post("/admin/control/start", tags=["admin"])
async def post_control_start() -> dict[str, Any]:
    """In Phase 2 this starts the Betfair stream + recording loop.
    Phase 1 returns a clear "shell only" notice."""

    return {
        "action": "start",
        "accepted": False,
        "shell_mode": True,
        "detail": "Phase 1 shell — recording logic not yet implemented",
    }


@app.post("/admin/control/stop", tags=["admin"])
async def post_control_stop() -> dict[str, Any]:
    return {
        "action": "stop",
        "accepted": False,
        "shell_mode": True,
        "detail": "Phase 1 shell — nothing to stop",
    }


@app.get("/admin/events", tags=["admin"])
async def get_events():
    """SSE stream. Phase 1 emits a single 'shell_mode' notice and exits."""

    async def event_iter():
        import asyncio
        yield {
            "event": "shell_mode",
            "data": (
                '{"detail":"Phase 1 shell — no recording events yet"}'
            ),
        }
        # Keep the connection alive with a heartbeat so the portal
        # doesn't see it as a hard close. 15s ping.
        for _ in range(20):
            await asyncio.sleep(15)
            yield {"event": "heartbeat", "data": '{"ts":"' + _iso_now() + '"}'}

    return EventSourceResponse(event_iter())


# ──────────────────────────────────────────────────────────────────────────────
# API — recordings list / metadata / replay / download
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/api/recordings", tags=["api"])
async def list_recordings() -> dict[str, Any]:
    """List all daily recording files. Phase 1 returns empty list."""

    return {
        "shell_mode": app.state.shell_mode,
        "recordings": [],
    }


@app.get("/api/recordings/{date}", tags=["api"])
async def get_recording_meta(date: str) -> dict[str, Any]:
    """Metadata for one day's recording.

    Phase 1 returns 404 for every date — no recordings exist yet.
    """

    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    raise HTTPException(
        status_code=404,
        detail=f"no recording for {date} (shell mode — no data yet)",
    )


@app.get("/api/replay/{date}", tags=["api"])
async def replay_recording(date: str):
    """Stream a day's NDJSON for replay. Phase 1 returns 404."""

    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    raise HTTPException(
        status_code=404,
        detail=f"no recording for {date} (shell mode — no data yet)",
    )


@app.get("/api/download/{date}", tags=["api"])
async def download_recording(date: str):
    """Download a day's NDJSON file. Phase 1 returns 404."""

    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    raise HTTPException(
        status_code=404,
        detail=f"no recording for {date} (shell mode — no data yet)",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", tags=["meta"])
async def ready() -> dict[str, Any]:
    """Readiness probe. In Phase 2 also checks Betfair session + GCS reach."""

    return {
        "status": "ok",
        "shell_mode": app.state.shell_mode,
        "betfair_session": "not_checked_in_shell",
        "gcs_reachable": "not_checked_in_shell",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
