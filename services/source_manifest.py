"""
Source Manifest registration (Bible §21).

Manifest lives at:
  gs://chimera-portal-config/source_manifest.json

The manifest is a shared dict keyed by FSU id. Each FSU registers
itself on startup; consumers read the manifest to discover where
each data source / venue edge lives.

FSU1B's entry follows the shape SC's Phase 4 go-ahead specifies:

  {
    "fsu1b": {
      "name": "Betfair Exchange Gateway",
      "type": "live_stream",
      "url": "https://fsu1b-XXXXXX.europe-west2.run.app",
      "sports": ["horse_racing", "football", "tennis"],
      "endpoints": { ... },
      "status": "active",
      "last_registered": "<UTC iso>"
    }
  }

Failure mode:
  Manifest registration is best-effort. If GCS is unreachable, log a
  warning and continue — the gateway is still functional, just not
  discoverable until the next registration succeeds. POST
  /admin/control/reregister_source forces a retry.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from core.config import get_settings

logger = logging.getLogger(__name__)


def _disabled() -> bool:
    return bool(os.environ.get("FSU1B_DISABLE_GCP_IO"))


# All of FSU1B's public endpoints — kept in one place so the manifest
# entry is the single source of truth for downstream consumers.
ENDPOINTS: dict[str, str] = {
    "stream_horse_racing": "/stream/horse-racing",
    "stream_football": "/stream/football",
    "stream_tennis": "/stream/tennis",
    "stream_all": "/stream/all",
    "snapshot": "/stream/snapshot",
    "markets": "/markets",
    "market_detail": "/markets/{id}",
    "orders_place": "/orders/place",
    "orders_cancel": "/orders/cancel",
    "orders_replace": "/orders/replace",
    "orders_current": "/orders/current",
    "orders_cleared": "/orders/cleared",
    "account_funds": "/account/funds",
    "account_statement": "/account/statement",
    "catalogue": "/catalogue/markets",
}

SPORTS: list[str] = ["horse_racing", "football", "tennis"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_manifest_entry() -> dict[str, Any]:
    """Construct the FSU1B entry. Uses current Settings (url, status)."""
    s = get_settings()
    return {
        "name": "Betfair Exchange Gateway",
        "type": "live_stream",
        "url": s.service_url,
        "sports": list(SPORTS),
        "endpoints": dict(ENDPOINTS),
        "status": "active",
        "last_registered": _now_iso(),
    }


def register() -> dict[str, Any]:
    """Read, merge our entry, write back. Returns the entry that was written.

    Raises on GCS failure. Caller wraps for best-effort semantics.
    """
    if _disabled():
        logger.info("FSU1B_DISABLE_GCP_IO set — returning entry without GCS write.")
        return build_manifest_entry()

    s = get_settings()
    bucket_name = s.manifest_bucket
    blob_name = s.manifest_blob

    from google.cloud import storage  # type: ignore[import-not-found]

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    if blob.exists():
        try:
            manifest = json.loads(blob.download_as_text())
            if not isinstance(manifest, dict):
                logger.warning("Manifest at gs://%s/%s is not an object — replacing.",
                               bucket_name, blob_name)
                manifest = {}
        except json.JSONDecodeError as exc:
            logger.warning("Manifest unreadable (%s) — starting fresh.", exc)
            manifest = {}
    else:
        manifest = {}

    entry = build_manifest_entry()
    manifest[s.service_name] = entry

    blob.upload_from_string(
        json.dumps(manifest, indent=2, sort_keys=True),
        content_type="application/json",
    )
    logger.info(
        "Source manifest updated: gs://%s/%s (fsu1b → url=%r, sports=%s)",
        bucket_name, blob_name, entry["url"], entry["sports"],
    )
    return entry


def register_best_effort() -> dict[str, Any] | None:
    """Try to register; swallow errors and return None on failure."""
    try:
        return register()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Source manifest registration failed: %s", exc)
        return None
