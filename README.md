# FSU1B — Betfair Stream Recorder

**Standalone tape recorder for Betfair MarketBook data.**

| | |
|---|---|
| FSU | **FSU1B** |
| Repo | `chimeracloud/fsu1b` |
| Cloud Run service | `fsu1b-stream-recorder` (`europe-west2`, project `chiops`) |
| Service account | `fsu1b-sa@chiops.iam.gserviceaccount.com` |
| GCS bucket | `gs://chiops-betfair-recording/` |

> **Naming convention note.** The service / SA / repo follow FSU naming.
> The bucket follows CHI-POL-009 (`chiops-{product}-{function}`) and
> stays as `chiops-betfair-recording`. The GCP service account ID
> minimum is 6 characters, so the SA is `fsu1b-sa` not `fsu1b` —
> matches the existing `fsu1e-sa` convention.

---

## What it does

Records raw Betfair MarketBook updates for GB / IE WIN horse-racing
markets as NDJSON, one line per update. It does **not** calculate
anything. No P&L, no commission, no aggregations, no derived values.
It writes what Betfair sends. Nothing else.

FSU1B runs independently of CLE V2. If CLE V2 crashes, redeploys, or
is stopped, FSU1B keeps recording. Both services maintain their own
Betfair stream subscription using the same credentials (Betfair
allows concurrent streams per account).

---

## Operating principles

1. **Pure capture, zero interpretation.** The job is to record what
   Betfair emits, byte-faithful. Any consumer (replay, audit, future
   backtest) can derive whatever they need from the raw stream.
2. **One service, one stream, one file per day.** Single Cloud Run
   instance, single Betfair subscription, single NDJSON file per
   UTC trading day. Rolls over at 12:00 UTC.
3. **Resume on restart.** If the service restarts mid-day, the
   recorder downloads today's existing file from GCS, appends to it
   locally, and continues. No data loss across restarts.
4. **Independent of CLE V2.** Same Betfair account, different stream
   connection. No shared state. No cascading failures.
5. **Auto-starts on boot.** Unlike CLE V2 (which boots STOPPED so it
   never silently places bets), FSU1B boots RECORDING. A recorder
   places no bets — the worst case of auto-recording is "more data
   captured", never "money lost". The operator should never have to
   wonder whether recording is on. See *Auto-start* below.

---

## File layout

| File | Purpose |
|---|---|
| `main.py` | FastAPI entrypoint — all admin / api / health endpoints |
| `settings.py` | `RecorderSettings` dataclass — market filter, flush intervals, rollover |
| `recorder.py` | Phase 2 — Betfair stream + NDJSON serialisation + buffer |
| `gcs.py` | Phase 2 — GCS upload + download + meta file writer |
| `auth.py` | Phase 2 — Betfair client builder (Secret Manager creds) |
| `Dockerfile` | Cloud Run build context |
| `cloudbuild.yaml` | Cloud Build trigger config |
| `requirements.txt` | Pinned Python deps |

All phases are live: real Betfair streaming, NDJSON serialisation,
GCS flush/rollover, and the CST portal page are deployed. Responses
report `shell_mode: false`.

---

## Data format — NDJSON

One JSON object per line. Each line is a self-contained MarketBook
snapshot at a moment in time.

```json
{
  "ts": "2026-05-15T13:45:01.234Z",
  "mid": "1.234567890",
  "event": "York",
  "name": "2:45 York",
  "status": "OPEN",
  "inplay": false,
  "total_matched": 45000.00,
  "runners": [
    {
      "sid": 12345,
      "name": "Horse Name",
      "ltp": 3.50,
      "back": [[3.55, 100], [3.50, 200], [3.45, 150]],
      "lay":  [[3.60,  50], [3.65,  80], [3.70, 120]],
      "status": "ACTIVE"
    }
  ]
}
```

Captured fields:
- `ts` — ISO 8601 UTC timestamp of capture (millisecond precision)
- `mid` — Betfair market_id
- `event` — venue name from `market_definition.event_name`
- `name` — market name from `market_definition.name`
- `status` — Betfair market status (`OPEN` / `SUSPENDED` / `CLOSED`)
- `inplay` — boolean from `market_definition.in_play`
- `total_matched` — `market_book.total_matched`
- `runners[]` — per-runner snapshot:
  - `sid` — selection_id
  - `name` — runner name (from market_definition.runners)
  - `ltp` — last_price_traded
  - `back` — best 3 back prices as `[price, size]` pairs
  - `lay` — best 3 lay prices as `[price, size]` pairs
  - `status` — runner status (`ACTIVE` / `REMOVED` / etc.)

This data is sufficient to replay a full session as if it were live —
the same MarketBook objects can be reconstructed from the NDJSON.

---

## Storage layout

`gs://chiops-betfair-recording/` (bucket name follows CHI-POL-009,
not FSU naming).

```
horse-racing/
├── 2026-05-15.ndjson           ← daily NDJSON file
├── 2026-05-15_meta.json        ← daily metadata
├── 2026-05-14.ndjson
├── 2026-05-14_meta.json
└── ...

settings/
└── current.json                ← persisted RecorderSettings (Phase 2)
```

**Meta file shape** (written on stop or rollover):

```json
{
  "date": "2026-05-15",
  "recording_start": "2026-05-15T12:00:00Z",
  "recording_end": "2026-05-16T12:00:00Z",
  "markets_recorded": 34,
  "total_updates": 125000,
  "file_size_bytes": 45000000,
  "venues": ["York", "Salisbury", "Perth", "Fontwell"]
}
```

---

## Endpoint surface

```
Admin:
  GET  /admin/status              recording active/inactive, current file, counts
  GET  /admin/config              market filter + schedule
  PUT  /admin/config              update settings
  GET  /admin/stats               updates/min, file size, markets count
  POST /admin/control/start       start recording
  POST /admin/control/stop        stop recording
  GET  /admin/events              SSE — recording events

API:
  GET  /api/recordings            list all daily recording files
  GET  /api/recordings/{date}     metadata for a specific day
  GET  /api/replay/{date}         stream NDJSON for that day
  GET  /api/download/{date}       download the full file

Health:
  GET  /health                    liveness
  GET  /ready                     readiness (Betfair + GCS reachable)
```

---

## Auto-start

FSU1B **boots into RECORDING**. On container start, `lifespan` spawns
a daemon thread (`fsu1b-autostart`) that calls `recorder.start()` with
backoff retry — 12 attempts, `min(30, 5·attempt)` seconds apart — so a
transient Secret Manager / Betfair-auth hiccup at boot doesn't leave
the recorder silently idle. The start runs off the request path, so
the container reports READY immediately even while Betfair login is
still in flight.

This is **deliberately the inverse of CLE V2**, which boots STOPPED so
it can never silently place bets after a deploy. The reasoning differs
because the risk differs:

| | CLE V2 | FSU1B |
|---|---|---|
| Boots | STOPPED | RECORDING |
| Worst case of auto-on | money lost (unintended bets) | more data captured |
| Worst case of auto-off | strategy paused (recoverable) | **data lost forever** |

A recorder places no bets. The only failure that actually costs
something is *not recording* — a gap in the tape can never be
back-filled. So the safe default is "always on". The operator should
never have to remember to press start, and a deploy mid-day simply
reconnects and **appends to the same trading-day file** (the recorder
downloads the existing GCS NDJSON on start, so the few seconds of
disconnect is the only loss, not the whole day).

To override for a one-off (e.g. a maintenance window), `PUT
/admin/config` with `{"auto_start": false}` then redeploy — but the
dataclass default is `True` and that is the intended steady state.
`GET /admin/status` reports the live `auto_start` value alongside
`recording`.

---

## Build + deploy

Cloud Build trigger (set up via GUI one-time step — see `cloudbuild.yaml`)
watches `main` branch, builds `Dockerfile`, deploys to Cloud Run
service `fsu1b-stream-recorder`.

Manual deploy:

```bash
gcloud run deploy fsu1b-stream-recorder \
  --source . \
  --region europe-west2 \
  --service-account fsu1b-sa@chiops.iam.gserviceaccount.com \
  --no-allow-unauthenticated \
  --memory 1Gi --cpu 1 \
  --min-instances 1 --max-instances 1 \
  --no-cpu-throttling \
  --set-env-vars GCP_PROJECT=chiops
```

`max-instances=1` is **required** — the recorder is stateful (single
Betfair stream subscription, single daily file). Multiple instances
would create duplicate streams and overwrite each other's writes.

---

## Phase status

| Phase | Status |
|---|---|
| Phase 1 — shell (all endpoints, no recording) | ✅ Deployed |
| Phase 2 — Betfair connection + NDJSON capture + GCS upload + rollover | ✅ Deployed |
| Phase 3 — portal Data Recorder page in CST | ✅ Deployed |
| Auto-start on boot (records by default, resumes day's file) | ✅ Deployed |

Smoke-tested end-to-end: a live session captured 1,647 updates across
90 markets / 12 venues, with the NDJSON file and meta sidecar written
to GCS and replayable via `/api/replay/{date}`.

---

## Why a separate service from CLE V2

**Independent failure domains.** CLE V2 makes decisions and places
bets. FSU1B captures data. They have different reliability needs.
CLE V2 can be down for hours while a strategy is tuned. FSU1B must
run continuously so we never lose data. Coupling them in one service
means a CLE V2 redeploy interrupts the recording — unacceptable.

**Different resource patterns.** CLE V2 is CPU-bound at decision
time (evaluator runs many comparisons per market). FSU1B is I/O
bound (serialise, buffer, write). Separating them lets each have
its own resource profile.

**Different deployment cadence.** CLE V2 will change often as
strategy evolves. FSU1B is set-and-forget. Each redeploy of FSU1B
costs a few seconds of stream disconnect — for the recorder, that's
a real loss. For CLE V2, it's irrelevant.
