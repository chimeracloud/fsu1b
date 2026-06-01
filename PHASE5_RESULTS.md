# FSU1B — Phase 5 Testing Results

**Date started:** _<fill in>_
**Operator:** _<fill in>_
**`$SERVICE_URL`:** _<fill in after DEPLOY.md step 10>_
**Gateway version under test:** `0.4.0-phase4`

> **DO NOT** run Test 6 (real £2 bet) until Tests 1–5 have all passed
> and Charles has given explicit go-ahead.

---

## Scaffold notes

For each test:

- **PASS** — all check items completed, evidence captured.
- **FAIL** — at least one check item failed. Note what failed, the evidence (log line, response body, screenshot), and the proposed fix.
- **BLOCKED** — couldn't run yet (e.g. dependent test hasn't passed; needed approval; off-peak window required).

Every `curl` below assumes:

```bash
TOKEN=$(gcloud auth print-identity-token)
SERVICE_URL=$(gcloud run services describe fsu1b \
  --region=europe-west2 --project=chiops --format='value(status.url)')
```

Stop at the first **FAIL**. Do not proceed to the next test until the failing test is resolved or explicitly waived.

---

## TEST 1 — Idle state

**Status:** _PASS / FAIL / BLOCKED_
**Run at:** _<UTC timestamp>_

### Commands
```bash
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/health" | jq .
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/ready" | jq .
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/status" | jq .
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/config" | jq .
```

### Expected
- `/health` → `{"status":"ok"}`
- `/ready` → `{"ready": true, "mode": "idle", "phase": 4, ...}` (HTTP 200)
- `/admin/status.live_session.state` → `"not_started"`
- `/admin/status.delayed_session.state` → `"not_started"`
- `/admin/status.stream.running` → `false`
- `/admin/config.event_type_ids` → `["7","1","2"]`
- `/admin/config.countries` → `["GB","IE"]`
- `/admin/config.dry_run` → `false`
- `/admin/config.auto_start` → `false`

### Evidence
_paste response bodies / log lines here_

### Notes
_anything unexpected_

---

## TEST 2 — LIVE key login + stream

**Status:** _PASS / FAIL / BLOCKED_
**Run at:** _<UTC timestamp — must be inside trading hours UK>_

### Commands
```bash
curl -X POST -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/control/start" | jq .

sleep 30

curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/status" | jq .
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/stats" | jq .
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/markets" | jq '.count, .markets[0]'
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/markets?sport=horse-racing" | jq .count
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/markets?sport=football" | jq .count
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/markets?sport=tennis" | jq .count

# SSE — give each ~5 seconds, then Ctrl-C
curl -N -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/stream/all"           | head -20
curl -N -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/stream/horse-racing"  | head -20
curl -N -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/stream/football"      | head -20
curl -N -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/stream/tennis"        | head -20
```

### Expected
- `/admin/control/start` → `{"accepted": true, "executed": true, ...}`
- After 30s:
  - `live_session.state` → `"active"`
  - `stream.stream_status` → `"connected"`
  - `stream.last_message_at` → recent ISO timestamp
- `/admin/stats.messages_per_s` → `> 0`
- `/markets.count` → some markets in cache (depends on time of day)
- Per-sport SSE only carries events for that sport — check `event_type_id` in each event body
- `/stream/all` SSE carries all three event_type_ids

### Evidence
_paste counts, sample SSE events showing event_type_id_

### Notes
_anything unexpected_

---

## TEST 3 — DELAYED key login + REST

**Status:** _PASS / FAIL / BLOCKED_
**Run at:** _<UTC timestamp>_

### Commands
```bash
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/account/funds" | jq .
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/orders/current" | jq '.betfair'
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/catalogue/markets?event_type_id=7&max_results=10" | jq '.betfair[0]'
```

### Expected
- `/account/funds.betfair.availableToBetBalance` → numeric (the real Betfair balance)
- `/orders/current` → returns envelope with `betfair` array (may be empty)
- `/catalogue/markets` → returns market list with runner names + market start times
- After these calls, `/admin/status.delayed_session.state` → `"active"`

### Evidence
_paste the account balance (REDACT to nearest £100) + a catalogue sample_

### Notes
_did the first call take longer than subsequent ones? (cert login overhead is expected on first call only)_

---

## TEST 4 — Watchdog

**Status:** _PASS / FAIL / BLOCKED_
**Run at:** _<UTC timestamp — must be inside trading hours so the stream is active>_

### Approach
Three options, in order of preference:

(a) **Wait for natural drop** — leave it running for the full 24-hour soak (Test 7) and check `/admin/activity` for any `event:gateway_stream_stale` or `event:gateway_reconnected` rows.

(b) **Force via the admin surface** — `POST /admin/control/reconnect_stream`. This is a manual force-disconnect (different code path from watchdog but exercises the supervisor reconnect logic):
```bash
curl -X POST -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/control/reconnect_stream" | jq .

# Then watch /admin/activity for the next 60s:
for i in 1 2 3 4 5 6; do
  sleep 10
  curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/activity" \
    | jq '.events[-5:]'
done
```

(c) **Simulate by lowering threshold** — temporarily set `stream_stale_threshold_s=5`, then wait 6 seconds and watch the watchdog trip. Restore the default afterwards.
```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"stream_stale_threshold_s": 5}' "$SERVICE_URL/admin/config"
sleep 35  # watchdog runs every 30s; needs one full cycle to trip
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/activity" | jq '.events[-10:]'
# RESTORE
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"stream_stale_threshold_s": 60}' "$SERVICE_URL/admin/config"
```

### Expected
- (b) or (c) produces `event:gateway_session_dropped` or `event:gateway_stream_stale` in activity
- Followed by `event:gateway_reconnected` (for watchdog-triggered) or `event:gateway_session_recovered` (for other) within a few seconds
- `/ready` returns **503** while the stream is stale; returns 200 after reconnect
- `reconnect_count` increments

### Evidence
_paste the activity log slice showing dropped → reconnected_

### Notes
_(c) is the deterministic option for this test. (a) is reality but takes 24h._

---

## TEST 5 — DRY_RUN bet

**Status:** _PASS / FAIL / BLOCKED_
**Run at:** _<UTC timestamp>_

### Commands
```bash
# Flip into DRY_RUN.
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"dry_run": true}' "$SERVICE_URL/admin/config" | jq .

# Pick a live market — note one selection_id from /markets.
MARKET_ID="<from /markets>"
SELECTION_ID=<from /markets/{MARKET_ID}>

# Place a £2 LAY at price 5.0.
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{
    \"market_id\": \"$MARKET_ID\",
    \"instructions\": [{
      \"selection_id\": $SELECTION_ID,
      \"side\": \"LAY\",
      \"order_type\": \"LIMIT\",
      \"limit_order\": {\"size\": 2.0, \"price\": 5.0, \"persistence_type\": \"LAPSE\"},
      \"customer_order_ref\": \"phase5-test-${RANDOM}\"
    }],
    \"customer_ref\": \"phase5-${RANDOM}\",
    \"customer_strategy_ref\": \"phase5-dryrun\"
  }" \
  "$SERVICE_URL/orders/place" | jq .

# Confirm nothing landed.
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/orders/current?market_id=$MARKET_ID" | jq .

# Restore.
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"dry_run": false}' "$SERVICE_URL/admin/config" | jq .
```

### Expected
- `/orders/place` returns `{"ok": true, "dry_run": true, ...}`
- The simulated `bet_id` (in `betfair.instructionReports[].betId`) starts with `DRY-`
- `/orders/current` after the call: no new order with that `customer_order_ref`
- Verify on Betfair web UI: no bet on the account for this market
- After restore, `/admin/config.dry_run` → `false`

### Evidence
_paste the DRY-… bet id and the empty /orders/current confirmation_

### Notes
_did the simulated bet survive a restart? (it shouldn't — DRY_RUN is purely in-memory)_

---

## TEST 6 — Real £2 bet (REQUIRES EXPLICIT OPERATOR APPROVAL)

**Status:** _PASS / FAIL / BLOCKED / NOT-RUN_
**Approved by Charles at:** _<UTC timestamp + how — voice / Slack / signed line below>_
**Run at:** _<UTC timestamp>_

> **DO NOT run this test without an explicit go-ahead. Live money.**
> Approval line: _________________________________________________

### Commands
```bash
# Ensure not in DRY_RUN.
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"dry_run": false}' "$SERVICE_URL/admin/config" | jq .

# Pick a horse-racing market with deep liquidity. Capture market_id + selection_id.
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/markets?sport=horse-racing" | jq .
MARKET_ID="<chosen>"
SELECTION_ID=<chosen — pick a runner mid-price>

# Place a £2 LAY at best available price + 1 tick (or whatever the strategy
# would normally do — keep it small + likely to match or sit on the book).
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{
    \"market_id\": \"$MARKET_ID\",
    \"instructions\": [{
      \"selection_id\": $SELECTION_ID,
      \"side\": \"LAY\",
      \"order_type\": \"LIMIT\",
      \"limit_order\": {\"size\": 2.0, \"price\": <PRICE>, \"persistence_type\": \"LAPSE\"},
      \"customer_order_ref\": \"phase5-live-${RANDOM}\"
    }],
    \"customer_ref\": \"phase5-real-${RANDOM}\",
    \"customer_strategy_ref\": \"phase5-real\"
  }" \
  "$SERVICE_URL/orders/place" | jq .

# Capture the betId from the response.
BET_ID="<from instructionReports[].betId>"

# Confirm it shows in current orders.
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/orders/current?market_id=$MARKET_ID" | jq .

# Cancel it.
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{
    \"market_id\": \"$MARKET_ID\",
    \"instructions\": [{\"bet_id\": \"$BET_ID\"}],
    \"customer_ref\": \"phase5-cancel-${RANDOM}\"
  }" \
  "$SERVICE_URL/orders/cancel" | jq .

# Confirm cancellation.
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/orders/current?market_id=$MARKET_ID" | jq .
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/account/funds" | jq .
```

### Expected
- `place` → `{"ok": true, "dry_run": false, "betfair": {"status": "SUCCESS", "instructionReports": [{"status": "SUCCESS", "betId": "<some-id>", ...}]}}`
- `betId` does NOT start with `DRY-`
- `/orders/current` shows the bet with the same `betId`
- `cancel` → `{"ok": true, "dry_run": false, "betfair": {"status": "SUCCESS", ...}}`
- After cancel, `/orders/current` shows no unmatched portion of the bet
- `/account/funds.availableToBetBalance` returns to its pre-bet value (if cancelled before any match)

### Evidence
_paste betId, the cancel confirmation, the funds delta_

### Notes
_did the order match before cancel? If so, the exposure remains until settlement — note this; settlement is the FSU2B job not FSU1B's._

---

## TEST 7 — 24-hour soak

**Status:** _PASS / FAIL / BLOCKED_
**Started:** _<UTC timestamp>_
**Ended:** _<UTC timestamp — should be exactly 24h later>_

### Approach
Leave the service running with the stream up. Snapshot `/admin/status`, `/admin/stats`, `/ready`, and `/admin/activity` at 4-hour intervals.

### Sampling schedule

| # | Time (UTC) | `/ready` | `/admin/stats.messages_per_s` | `reconnect_count` | Notes |
|---|---|---|---|---|---|
| 0 | _<start>_       | _200/503_ | _<rate>_ | _<n>_ | start of soak |
| 1 | _<start +4h>_   | | | | |
| 2 | _<start +8h>_   | | | | |
| 3 | _<start +12h>_  | | | | |
| 4 | _<start +16h>_  | | | | |
| 5 | _<start +20h>_  | | | | |
| 6 | _<start +24h>_  | | | | end of soak |

### End-of-soak

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/status" | jq .
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/stats" | jq .
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/activity" | jq .

# Verify the daily_summary event fired (look for the most recent
# event:gateway_daily_summary in activity).
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/activity" \
  | jq '.events[] | select(.kind == "event:gateway_daily_summary")'

# Also check Pub/Sub if a subscription has been created:
# gcloud pubsub subscriptions pull <sub-name> --auto-ack --limit=20 --project=chiops
```

### Pass criteria
- Zero unrecovered drops (every `gateway_session_dropped` is followed by `gateway_session_recovered` within minutes)
- `reconnect_count` low (single digits, ideally < 5 over 24h)
- `messages_per_s` non-zero during trading hours
- `gateway_daily_summary` event fired at 00:00 UTC
- `/ready` 200 every sample

### Evidence
_paste the table, the daily_summary event, and any unexpected activity_

### Notes
_off-peak drops in messages_per_s are normal — only flag if `connected` state was lost_

---

## TEST 8 — GCS config persistence

**Status:** _PASS / FAIL / BLOCKED_
**Run at:** _<UTC timestamp>_

### Commands
```bash
# Note the current value.
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/config" | jq .stream_stale_threshold_s

# Change it.
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"stream_stale_threshold_s": 90}' "$SERVICE_URL/admin/config" | jq .

# Verify GCS has the new value.
gcloud storage cat gs://chiops-betfair-recording/config/fsu1b.json \
  | jq .stream_stale_threshold_s
# expected: 90

# Force a Cloud Run restart by updating an unrelated label (no rebuild).
gcloud run services update fsu1b \
  --region=europe-west2 --project=chiops \
  --update-labels=phase5-test=$(date +%s)

# Wait for the new revision to be ready.
gcloud run revisions list --service=fsu1b \
  --region=europe-west2 --project=chiops --limit=2

# Re-check the in-memory setting after restart.
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/config" | jq .stream_stale_threshold_s
# expected: 90  (persisted!)

# Restore.
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"stream_stale_threshold_s": 60}' "$SERVICE_URL/admin/config" | jq .
```

### Expected
- After PUT, GCS blob has the new value
- After service restart, `/admin/config` returns the persisted value
- After restore PUT, both in-memory and GCS reflect the default

### Evidence
_paste the GCS blob excerpt and the post-restart `/admin/config` output_

### Notes
_did the restart preserve the LIVE / DELAYED sessions? (it shouldn't — Cloud Run replaces the container; the new container will re-login when started)_

---

## Production-readiness summary

> Fill this in once Tests 1–8 are all complete.

| # | Test | Status | Notes |
|---|---|---|---|
| 1 | Idle state | _PASS/FAIL_ | |
| 2 | LIVE login + stream | | |
| 3 | DELAYED login + REST | | |
| 4 | Watchdog | | |
| 5 | DRY_RUN bet | | |
| 6 | Real £2 bet | | _operator approval required_ |
| 7 | 24-hour soak | | |
| 8 | GCS config persistence | | |

**Ready for production: _YES / NO_**

**Outstanding issues:** _list, with severity_

**Recommendations:** _what to address before / during the cut-over_

**Sign-off:** _Charles, signed at <UTC timestamp>_

---

*Born from complexity. Engineered for certainty.*
