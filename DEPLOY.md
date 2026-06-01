# FSU1B — Deploy Checklist

> Operator runs each command manually. Verify after every step. No automation.
> Stop at the first failure and resolve before continuing.

**Project / region (locked)**: `chiops` / `europe-west2`
**Service account (locked)**: `fsu1b@chiops.iam.gserviceaccount.com`
**Cloud Run service name (locked)**: `fsu1b`
**Default branch on GitHub at time of deploy**: `main` (after the branch swap in step 9)

---

## PRE-DEPLOY — GCP setup

### 1. Create the portal-config bucket (if not exists)

```bash
gcloud storage buckets create gs://chimera-portal-config \
  --project=chiops \
  --location=europe-west2
```

Idempotency: if it already exists, `gcloud` exits 1 with `HTTPError 409: You already own this bucket`. Safe to ignore.

**Verify:**
```bash
gcloud storage ls gs://chimera-portal-config/
```
Expected: no error (empty listing is fine).

---

### 2. Grant FSU1B SA access to portal-config bucket

```bash
gcloud storage buckets add-iam-policy-binding gs://chimera-portal-config \
  --member="serviceAccount:fsu1b@chiops.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

**Verify:**
```bash
gcloud storage buckets get-iam-policy gs://chimera-portal-config \
  --format="value(bindings)" | grep fsu1b
```
Expected: line mentioning `fsu1b@chiops.iam.gserviceaccount.com` and `roles/storage.objectAdmin`.

---

### 3. Grant FSU1B SA access to the recording bucket

```bash
gcloud storage buckets add-iam-policy-binding gs://chiops-betfair-recording \
  --member="serviceAccount:fsu1b@chiops.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

Bucket should already exist (it holds the existing `config/fsu1b.json` from the prior session). If not, create it first:
```bash
gcloud storage buckets create gs://chiops-betfair-recording \
  --project=chiops --location=europe-west2
```

**Verify:**
```bash
gcloud storage buckets get-iam-policy gs://chiops-betfair-recording \
  --format="value(bindings)" | grep fsu1b
```

---

### 4. Create the Pub/Sub topic

```bash
gcloud pubsub topics create chimera-fsu1b-events --project=chiops
```

Idempotency: existing topic returns `ALREADY_EXISTS`. Safe to ignore.

**Verify:**
```bash
gcloud pubsub topics list --project=chiops --format="value(name)" | grep fsu1b
```
Expected: `projects/chiops/topics/chimera-fsu1b-events`.

---

### 5. Grant FSU1B SA publisher role on the topic

```bash
gcloud pubsub topics add-iam-policy-binding chimera-fsu1b-events \
  --member="serviceAccount:fsu1b@chiops.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher" \
  --project=chiops
```

**Verify:**
```bash
gcloud pubsub topics get-iam-policy chimera-fsu1b-events --project=chiops \
  --format="value(bindings)" | grep fsu1b
```

---

### 6. Verify Secret Manager secrets exist

All six secrets must exist in the `chiops` project:

| Secret | Used by |
|---|---|
| `betfair-username` | both keys |
| `betfair-password` | both keys |
| `betfair-cert-pem` | both keys |
| `betfair-key-pem` | both keys |
| `betfair-app-key-live` | LIVE — stream + order placement |
| `betfair-app-key-delayed` | DELAYED — read-only REST |

**Verify:**
```bash
for SECRET in betfair-username betfair-password betfair-cert-pem \
              betfair-key-pem betfair-app-key-live betfair-app-key-delayed; do
  gcloud secrets describe $SECRET --project=chiops >/dev/null 2>&1 \
    && echo "$SECRET OK" \
    || echo "$SECRET MISSING";
done
```

Expected: six `OK` lines. Any `MISSING` blocks the deploy — create the secret before proceeding.

---

### 7. Grant FSU1B SA `secretAccessor` on every secret

```bash
for SECRET in betfair-username betfair-password betfair-cert-pem \
              betfair-key-pem betfair-app-key-live betfair-app-key-delayed; do
  gcloud secrets add-iam-policy-binding $SECRET \
    --member="serviceAccount:fsu1b@chiops.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" \
    --project=chiops;
done
```

**Verify (spot check):**
```bash
gcloud secrets get-iam-policy betfair-app-key-live --project=chiops \
  --format="value(bindings)" | grep fsu1b
```

---

### 8. Note the old FSU1B deployment (do not delete yet)

```bash
gcloud run services describe fsu1b-stream-recorder \
  --region=europe-west2 --project=chiops 2>/dev/null \
  && echo "OLD SERVICE EXISTS — decommission AFTER new FSU1B is verified" \
  || echo "OLD SERVICE ALREADY GONE — nothing to clean up"
```

**Action:** if it exists, leave it running until the new `fsu1b` service has passed Phase-5 testing. Decommission only after step 14 verifies the new service end-to-end. See `Decommissioning the legacy recorder` below.

---

## DEPLOY

### 9. Promote the GitHub default branch

The phase-4-integration branch is the canonical Phase 4 code. Make it main, archive the old main, then deploy from main.

```bash
# Archive the old main (26-May Opus 4.7 single-key gateway).
gh api -X PATCH repos/chimeracloud/fsu1b \
  --field default_branch=phase-4-integration

# Rename old main → legacy-phase2-singlekey.
git push origin refs/remotes/origin/main:refs/heads/legacy-phase2-singlekey
gh api -X DELETE repos/chimeracloud/fsu1b/git/refs/heads/main

# Rename phase-4-integration → main.
git fetch origin
git push origin refs/remotes/origin/phase-4-integration:refs/heads/main

# Switch default back to main.
gh api -X PATCH repos/chimeracloud/fsu1b --field default_branch=main

# Clean up the intermediate phase-N-* branches if desired (keep
# legacy-recorder and legacy-phase2-singlekey for audit).
```

**Verify:**
```bash
gh api repos/chimeracloud/fsu1b --jq '.default_branch'
# expected: "main"

gh api repos/chimeracloud/fsu1b/branches --jq '.[].name'
# expected: includes main, legacy-recorder, legacy-phase2-singlekey
```

---

### 10. Deploy to Cloud Run

```bash
cd ~/Projects/fsu1b

gcloud run deploy fsu1b \
  --source=. \
  --project=chiops \
  --region=europe-west2 \
  --service-account=fsu1b@chiops.iam.gserviceaccount.com \
  --no-allow-unauthenticated \
  --min-instances=1 \
  --max-instances=1 \
  --cpu=1 \
  --memory=512Mi \
  --cpu-boost \
  --no-cpu-throttling \
  --port=8080
```

> **Notes**
> - `--no-allow-unauthenticated` enforces IAM at the edge (CHI-POL-004).
> - `--min-instances=1` + `--no-cpu-throttling` keep the persistent stream alive between requests (Cloud Run would otherwise idle the container).
> - `--max-instances=1` — only one process can hold each Betfair session; Cloud Run scaling would break the stream invariant.
> - Do **NOT** set `FSU1B_DISABLE_GCP_IO` in env. The gateway needs real GCP I/O in production. Tests set it; production does not.
> - Capture the URL from the deploy output (or step 11).

**Verify exit code and the URL:**
```bash
echo "exit=$?"
SERVICE_URL=$(gcloud run services describe fsu1b \
  --region=europe-west2 --project=chiops \
  --format='value(status.url)')
echo "SERVICE_URL=$SERVICE_URL"
```

---

### 11. Set SERVICE_URL env var (second deploy)

Cloud Run only knows the URL after first deploy. The Source Manifest registration needs that URL to advertise FSU1B's location, so the operator either (a) re-deploys with the URL injected or (b) flips it via `PUT /admin/config` and then `POST /admin/control/reregister_source`.

**Path (a) — preferred, set it at deploy time:**
```bash
gcloud run services update fsu1b \
  --region=europe-west2 --project=chiops \
  --update-env-vars="SERVICE_URL=$SERVICE_URL"
```

Cloud Run will redeploy a new revision; the lifespan picks up `$SERVICE_URL` and propagates it into Settings.

**Path (b) — set it via the admin surface:**
```bash
TOKEN=$(gcloud auth print-identity-token)
curl -X PUT "$SERVICE_URL/admin/config" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"service_url\": \"$SERVICE_URL\"}"
curl -X POST "$SERVICE_URL/admin/control/reregister_source" \
  -H "Authorization: Bearer $TOKEN"
```

Path (b) also persists the URL to GCS so subsequent deploys pick it up automatically.

---

### 12. Verify the deploy (curl from your laptop)

```bash
TOKEN=$(gcloud auth print-identity-token)

curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/health"   ; echo
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/ready"    ; echo
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/info"     ; echo
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/admin/status" | jq .
```

Expected:
- `/health` → `{"status":"ok"}`
- `/ready`  → `{"ready":true, "mode":"idle", ...}`
- `/info`   → `{"service":"fsu1b","phase":4,...}`
- `/admin/status` → both sessions `not_started`, `stream.running=false`

If you get `401` or `403`, the IAM token is missing or your account doesn't have `roles/run.invoker`. Resolve before proceeding.

---

## POST-DEPLOY

### 13. Wire the portal proxy (cst-api repo)

cst-api lives in the `chimera-portal-api` repo. The browser never talks to FSU1B directly (CHI-ADR-014).

Two edits in cst-api:

1. **IAM** — grant cst-api's SA `roles/run.invoker` on the FSU1B service:
   ```bash
   gcloud run services add-iam-policy-binding fsu1b \
     --region=europe-west2 --project=chiops \
     --member="serviceAccount:cst-api@chiops.iam.gserviceaccount.com" \
     --role="roles/run.invoker"
   ```
2. **PROXY_TARGETS** — add `fsu1b` to the proxy target map:
   ```js
   // chimera-portal-api/src/proxy_targets.js (or equivalent)
   const PROXY_TARGETS = {
     // ...existing entries...
     fsu1b: process.env.FSU1B_URL,  // = $SERVICE_URL from step 10
   };
   ```
   Then redeploy cst-api.

**Verify:**

```bash
# From a browser session at chimerasportstrading.com:
# Open Products → Betfair → Gateway
# Inspect Network: requests should be POST /api/proxy/fsu1b/admin/status
# Response should match what step 12 showed for /admin/status
```

---

### 14. Force a Source Manifest re-register

After step 11 the URL is in settings, but the manifest may have been written before the URL was known. Force a fresh write:

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -X POST "$SERVICE_URL/admin/control/reregister_source" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

Expected: `{"action":"reregister_source","accepted":true,"executed":true,...}`.

**Verify the manifest:**
```bash
gcloud storage cat gs://chimera-portal-config/source_manifest.json | jq .fsu1b
```

Expected: an `fsu1b` entry with `url` matching `$SERVICE_URL`, three `sports`, all 15 endpoints listed.

---

## Decommissioning the legacy recorder

ONLY after Phase-5 testing in `PHASE5_RESULTS.md` passes:

```bash
# Take the legacy service offline.
gcloud run services delete fsu1b-stream-recorder \
  --region=europe-west2 --project=chiops --quiet

# Archive its repo branch (already done — `legacy-recorder` branch on
# chimeracloud/fsu1b stays as the audit trail).
```

---

## Rollback

If anything goes wrong during deploy:

```bash
# List revisions:
gcloud run revisions list --service=fsu1b \
  --region=europe-west2 --project=chiops

# Roll back to a known-good revision:
gcloud run services update-traffic fsu1b \
  --region=europe-west2 --project=chiops \
  --to-revisions=fsu1b-<previous-revision>=100
```

For a complete teardown (only if FSU1B becomes hostile):

```bash
gcloud run services delete fsu1b \
  --region=europe-west2 --project=chiops --quiet
```

The Betfair credentials in Secret Manager are not removed by service deletion. Old `fsu1b-stream-recorder` can be brought back to life if needed (its repo branch is `legacy-recorder`).

---

## Idempotency notes

- Steps 1, 4, 6: idempotent (errors on existing resource are safe to ignore).
- Steps 2, 3, 5, 7: idempotent (re-binding the same role is a no-op).
- Step 8: read-only.
- Step 9: destructive on `main`. Old main is preserved as `legacy-phase2-singlekey`.
- Step 10: replaces the running service. Step 11 redeploys again. Both are revisions, both rollback-able.
- Steps 13, 14: idempotent.

---

## Operator checklist (tick as you go)

- [ ] 1. `gs://chimera-portal-config` exists
- [ ] 2. FSU1B SA has `objectAdmin` on `chimera-portal-config`
- [ ] 3. FSU1B SA has `objectAdmin` on `chiops-betfair-recording`
- [ ] 4. `chimera-fsu1b-events` Pub/Sub topic exists
- [ ] 5. FSU1B SA has `pubsub.publisher` on the topic
- [ ] 6. All six Betfair secrets exist
- [ ] 7. FSU1B SA has `secretAccessor` on all six
- [ ] 8. Old `fsu1b-stream-recorder` noted
- [ ] 9. `main` is the Phase 4 code; old main archived as `legacy-phase2-singlekey`
- [ ] 10. `gcloud run deploy fsu1b` exited 0; `$SERVICE_URL` captured
- [ ] 11. `SERVICE_URL` env var set on the service
- [ ] 12. `/health`, `/ready`, `/info`, `/admin/status` all 200
- [ ] 13. cst-api `PROXY_TARGETS` updated + IAM granted; portal can reach the gateway
- [ ] 14. Source Manifest contains `fsu1b` entry with correct URL

Once all 14 are ticked, proceed to `PHASE5_RESULTS.md` for the test execution.

---

*Born from complexity. Engineered for certainty.*
