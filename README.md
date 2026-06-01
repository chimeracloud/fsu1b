# FSU1B — Betfair Exchange Gateway

> Born from complexity. Engineered for certainty.

Chimera's single Betfair Exchange edge. One FSU, one function: be the door to Betfair. Stream IN, REST IN, orders OUT.

Everything that needs to talk to Betfair Exchange — for any Chimera product — goes through this FSU. Recording, calculation, reporting, strategy, decisioning all live in other FSUs.

## Scope

| Layer | Key | What |
|---|---|---|
| Stream IN | LIVE | Betfair Exchange Stream API → per-sport SSE downstream |
| REST IN | DELAYED | account funds, current orders, cleared orders, market catalogue |
| Orders OUT | LIVE | place / cancel / replace (LIVE-key wagering activity) |
| Out of scope | — | Recording, calculation, strategy, decisioning, GUI rendering |

Full architecture: `audit/reports/FSU1B_Betfair_Gateway_Architecture.md`.

## Phases (CHI-POL-008)

| Phase | Status | What |
|---|---|---|
| 1 — Shell | done | Standard FSU shell, no Betfair |
| 2 — Stream + all sports | done | LIVE key, eventTypeIds 7/1/2, per-sport SSE, watchdog |
| 3 — REST | done | DELAYED-key reads + LIVE-key writes + DRY_RUN + pause/resume |
| 4 — Integration | in progress | GCS config + Source Manifest + Pub/Sub envelopes + portal proxy |
| 5 — Testing | pending | 24h soak, DRY_RUN parity, £2 live bet |

## Running locally (Phase 1)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn main:app --reload --port 8080
```

```bash
curl localhost:8080/health
curl localhost:8080/ready
curl localhost:8080/info
curl localhost:8080/metrics
curl localhost:8080/status
curl localhost:8080/admin/status
curl localhost:8080/admin/config
```

## Tests (Phase 1)

```bash
pytest -q
```

## Phase 2 endpoints

### Standard observability (identical across all FSUs)

- `GET /health` — liveness
- `GET /ready` — readiness; 503 when session running but stream stale
- `GET /info` — service identity
- `GET /metrics` — Prometheus plaintext (uptime, mcm total, reconnects, recent rate, stream age)
- `GET /status` — composite status

### Set 1 — admin (CHI-ADR-010)

- `GET /admin/status` — real LIVE-session + stream state + subscription counts
- `GET /admin/config`
- `PUT /admin/config` — in-memory only (Phase 4 wires GCS persistence)
- `GET /admin/stats`
- `GET /admin/activity`
- `POST /admin/control/{start|stop|reconnect_stream|pause|resume|relogin_rest|test}` — all wired
- `GET /admin/events` — SSE (real events in Phase 4)

### Set 3 — stream + market reads (Phase 2)

- `GET /stream/horse-racing` — SSE per-sport (eventTypeId 7)
- `GET /stream/football` — SSE per-sport (eventTypeId 1)
- `GET /stream/tennis` — SSE per-sport (eventTypeId 2)
- `GET /stream/all` — SSE firehose
- `GET /stream/snapshot?sport=&event_type_id=` — full cache JSON for cold-start
- `GET /markets?sport=&event_type_id=&status=` — summary list
- `GET /markets/{market_id}` — full market state with runner ladders

### Set 3 — REST reads (Phase 3, DELAYED key)

- `GET /orders/current?market_id=&customer_order_refs=&customer_strategy_refs=&from_record=&record_count=`
- `GET /orders/cleared?bet_status=&market_id=&settled_from=&settled_to=&from_record=&record_count=`
- `GET /account/funds?wallet=UK`
- `GET /account/statement?item_date_from=&item_date_to=&include_item=&from_record=&record_count=`
- `GET /catalogue/markets?event_type_id=&country=&market_type=&max_results=&sort=&in_play_only=`

### Set 3 — order writes (Phase 3, LIVE key — wagering activity)

- `POST /orders/place` — body: `PlaceOrdersRequest`
- `POST /orders/cancel` — body: `CancelOrdersRequest`
- `POST /orders/replace` — body: `ReplaceOrdersRequest`

All write endpoints respect `Settings.dry_run` (PUT /admin/config).
When `dry_run=true`, no Betfair call is made; the response is
simulated with `betfair.status=SUCCESS`. Upstream errors are returned
as 502 with a structured `{ok:false, upstream:'betfair', error, message}`.

### Order kill-switch (Phase 3)

- `POST /admin/control/pause` — write endpoints return 503; reads + stream unaffected
- `POST /admin/control/resume` — re-enable writes
- `POST /admin/control/relogin_rest` — force fresh DELAYED-key certlogin

### Integration (Phase 4)

#### GCS-backed config (CHI-POL-006)

- Bucket: `gs://chiops-betfair-recording/config/fsu1b.json` (configurable via Settings)
- On startup the gateway hydrates settings from the blob; missing blob → defaults written.
- `PUT /admin/config` persists immediately. 502 if GCS write fails (in-memory change still applies; retry).
- GCS unreachable on startup → log warning + use in-memory defaults; service still serves.

Service-account requirement:
```
fsu1b@chiops.iam.gserviceaccount.com  needs  roles/storage.objectAdmin
  on bucket  chiops-betfair-recording
```

#### Source Manifest (Bible §21)

- Manifest: `gs://chimera-portal-config/source_manifest.json`
- FSU1B registers itself under the `fsu1b` key on startup (best-effort).
- `POST /admin/control/reregister_source` forces a retry (e.g. after fixing IAM).
- The deploy injects `SERVICE_URL` env var; the lifespan propagates it into Settings so the manifest entry advertises the correct Cloud Run URL. (Env vars only carry deploy-time identity — never tunable settings.)

Service-account requirement:
```
fsu1b@chiops.iam.gserviceaccount.com  needs  roles/storage.objectAdmin
  on bucket  chimera-portal-config
```

#### Event envelope publishing (Bible §20)

- Pub/Sub topic: `chimera-fsu1b-events` (in chiops project; configurable)
- Envelope: `{envelope:{source,event_type,timestamp,version}, payload:{...}}`
- Event types: `gateway_started`, `gateway_stopped`, `gateway_session_dropped`, `gateway_session_recovered`, `gateway_stream_stale`, `gateway_reconnected`, `gateway_daily_summary`
- FSU1B emits **infrastructure** events only. Order business events belong to Live Betting Control (ADR-018).
- Stub fallback: when Pub/Sub is unavailable, envelopes are logged to stdout and broadcast on `/stream/all` SSE so portal operators still see them.

Service-account requirement:
```
fsu1b@chiops.iam.gserviceaccount.com  needs  roles/pubsub.publisher
  on topic  chimera-fsu1b-events
```

#### Portal proxy (CHI-ADR-014 / CHI-POL-006)

The browser never talks to FSU1B directly. The CST portal → `cst-api` proxy → FSU1B.

**FSU1B-side requirements (already enforced)**:
- No CORS middleware. Direct browser requests are not supported.
- Deployed `--no-allow-unauthenticated` (CHI-POL-004). Cloud Run rejects requests without a valid IAM ID token at the edge.
- All non-2xx responses carry structured `detail` so the proxy can surface meaningful errors to the operator instead of generic 500s.

**cst-api-side requirements (deployment-time, lives in chimera-portal-api repo)**:
- Service account `cst-api@chiops.iam.gserviceaccount.com` needs `roles/run.invoker` on the `fsu1b` Cloud Run service.
- Add `fsu1b` to `PROXY_TARGETS` so the portal can reach `/admin/*`, `/markets`, `/stream/*`, `/orders/*`, `/account/*`, `/catalogue/*` via the proxy.

#### Local development

To run tests / boot the gateway locally without touching real GCP:

```bash
export FSU1B_DISABLE_GCP_IO=1
uvicorn main:app --port 8080
```

When set, GCS config load/save, Source Manifest registration, and Pub/Sub publishing all short-circuit to safe no-ops. Pytest sets it automatically (`tests/conftest.py`).

## Naming

- Repo: `chimeracloud/fsu1b`
- Cloud Run service: `fsu1b`
- Service account: `fsu1b@chiops.iam.gserviceaccount.com`
- Region: `europe-west2`
- Config: GCS, portal-editable (no env vars for settings)
- Credentials: Secret Manager only

## References

- CHI-POL-003 — Credentials in Secret Manager
- CHI-POL-004 — `--no-allow-unauthenticated`
- CHI-POL-005 — FSU Build Workflow
- CHI-POL-006 / CHI-ADR-014 — Portal proxy pattern
- CHI-POL-008 — Shell-First Build Policy
- CHI-ADR-010 — Three Endpoint Sets
- CHI-ADR-013 — One task, one job
- CHI-ADR-015 .. 023 — FSU1B-specific decisions (Bible)
- Bible Section 20 — Event Envelope + Courier
- Bible Section 21 — Source Manifest
- Bible Section 24 — Data Capture Architecture
