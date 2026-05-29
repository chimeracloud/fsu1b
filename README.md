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
| 1 — Shell | in progress | Standard FSU shell, no Betfair |
| 2 — Stream + all sports | pending | LIVE key, eventTypeIds 7/1/2, per-sport SSE |
| 3 — REST | pending | DELAYED-key reads + LIVE-key writes |
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

## Phase 1 endpoints

### Standard observability (identical across all FSUs)

- `GET /health` — liveness
- `GET /ready` — readiness (Phase 2+ gates on stream freshness + REST auth)
- `GET /info` — service identity
- `GET /metrics` — Prometheus plaintext
- `GET /status` — composite status

### Set 1 — admin (CHI-ADR-010)

- `GET /admin/status`
- `GET /admin/config`
- `PUT /admin/config`
- `GET /admin/stats`
- `GET /admin/activity`
- `POST /admin/control/{start|stop|pause|resume|reconnect_stream|relogin_rest|test}`
- `GET /admin/events` — SSE

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
