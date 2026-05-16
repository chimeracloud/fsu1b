"""FSU1B — Betfair Stream Recorder — FastAPI entrypoint (PHASE 2).

Phase 2: real recording wired in. The Recorder owns the Betfair
stream, the NDJSON buffer, and the GCS flush/rollover lifecycle.
This module is just the HTTP surface over it.

Architectural rules from the brief, still in force:

* This service is a TAPE RECORDER. It records, it does not
  calculate. No P&L, no aggregation, no derived values.
* It runs independently of CLE V2. CLE V2 crashing or redeploying
  has no effect on this service. Separate Betfair stream
  subscription, separate GCS bucket, separate service account.
* Daily file rolls at 12:00 UTC. New file = new day.
* On restart mid-day, append to the same daily file (the Recorder
  downloads the existing GCS file to /tmp on start).

The recorder does NOT auto-start on boot. The operator must call
POST /admin/control/start. This is deliberate — a deploy should
never silently begin a new recording stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

import gcs
from recorder import Recorder
from settings import RecorderSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.recorder = Recorder(RecorderSettings())
    logger.info(
        "FSU1B live (Phase 2). Recorder NOT started — POST "
        "/admin/control/start to begin recording."
    )
    try:
        yield
    finally:
        app.state.recorder.shutdown()
        logger.info("FSU1B shut down")


app = FastAPI(
    title="FSU1B — Chimera Betfair Stream Recorder",
    description=(
        "Records raw Betfair MarketBook data as NDJSON for replay and "
        "audit. Pure capture — no calculations, no P&L, no aggregation. "
        "Independent of CLE V2."
    ),
    version="0.2.0",
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
    return app.state.recorder.status()


@app.get("/admin/config", tags=["admin"])
async def get_config() -> dict[str, Any]:
    return app.state.recorder.settings.to_dict()


@app.put("/admin/config", tags=["admin"])
async def put_config(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Replace recorder settings.

    Settings changes take effect on the NEXT recording session — the
    market filter / flush cadence can't change mid-stream without
    re-subscribing. Operator should stop, update config, start.
    """

    try:
        new_settings = RecorderSettings.from_dict(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid settings: {exc}") from exc
    app.state.recorder.replace_settings(new_settings)
    return {
        "settings": new_settings.to_dict(),
        "note": "applies to next recording session — stop + start to take effect",
    }


@app.get("/admin/stats", tags=["admin"])
async def get_stats() -> dict[str, Any]:
    return app.state.recorder.stats()


@app.post("/admin/control/start", tags=["admin"])
async def post_control_start() -> dict[str, Any]:
    """Begin recording. Idempotent."""

    return app.state.recorder.start()


@app.post("/admin/control/stop", tags=["admin"])
async def post_control_stop() -> dict[str, Any]:
    """Stop recording. Flushes remaining buffer + writes meta sidecar."""

    return app.state.recorder.stop()


@app.get("/admin/events", tags=["admin"])
async def get_events():
    """SSE — emits a status frame every 5s while the connection is open.

    The recorder doesn't have a discrete event bus (it's a tape
    recorder, not a decision engine), so this streams periodic
    status snapshots — enough for the portal to render a live tile
    without polling.
    """

    async def event_iter():
        while True:
            status = app.state.recorder.status()
            yield {
                "event": "status",
                "data": json.dumps(status, default=str),
            }
            await asyncio.sleep(5)

    return EventSourceResponse(event_iter())


# ──────────────────────────────────────────────────────────────────────────────
# API — recordings list / metadata / replay / download
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/api/recordings", tags=["api"])
async def list_recordings() -> dict[str, Any]:
    """List all daily recording files in the bucket, newest first."""

    try:
        recs = gcs.list_recordings()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"GCS list failed: {exc}") from exc
    # Enrich with meta sidecar info where available
    for r in recs:
        try:
            meta = gcs.get_meta(r["date"])
            if meta:
                r["markets_recorded"] = meta.get("markets_recorded")
                r["total_updates"] = meta.get("total_updates")
                r["venues"] = meta.get("venues", [])
        except Exception:  # noqa: BLE001
            pass
    return {"recordings": recs}


@app.get("/api/recordings/{date}", tags=["api"])
async def get_recording_meta(date: str) -> dict[str, Any]:
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    meta = gcs.get_meta(date)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"no recording for {date}")
    return meta


@app.get("/api/replay/{date}", tags=["api"])
async def replay_recording(date: str):
    """Stream the day's NDJSON line-by-line for replay."""

    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    def line_iter():
        any_lines = False
        for line in gcs.stream_ndjson(date):
            any_lines = True
            yield line + "\n"
        if not any_lines:
            # Nothing streamed — surface a 404-ish marker in-band since
            # we've already started a 200 stream.
            yield json.dumps({"error": f"no recording for {date}"}) + "\n"

    return StreamingResponse(line_iter(), media_type="application/x-ndjson")


@app.get("/api/download/{date}", tags=["api"])
async def download_recording(date: str) -> dict[str, Any]:
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    url = gcs.download_url(date)
    if url is None:
        raise HTTPException(status_code=404, detail=f"no recording for {date}")
    return {"date": date, "gcs_uri": url, "note": "use /api/replay for streaming access"}


# ──────────────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", tags=["meta"])
async def ready() -> dict[str, Any]:
    """Readiness — GCS reachable. Betfair session only checked while
    recording (we don't hold an idle session)."""

    gcs_ok = True
    try:
        gcs.list_recordings()
    except Exception:  # noqa: BLE001
        gcs_ok = False
    rec = app.state.recorder
    return {
        "status": "ok" if gcs_ok else "degraded",
        "recording": rec.is_recording,
        "stream_status": rec.status().get("stream_status"),
        "gcs_reachable": gcs_ok,
    }


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
