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
| 3 — REST | in progress | DELAYED-key reads + LIVE-key writes + DRY_RUN + pause/resume |
| 4 — Integration | pending | Portal proxy, envelope, manifest, registries |
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
