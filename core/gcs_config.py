"""
GCS-backed config persistence (Phase 4).

Per CHI-POL-006: portal-editable config lives in GCS, never in env
vars. On startup we hydrate `core.config._current` from the GCS blob;
on every `PUT /admin/config` we write the new state back. The
infrastructure config (bucket name, blob path) is in `Settings`
itself with sensible defaults.

Bucket / blob defaults match the SC go-ahead:
  gs://chiops-betfair-recording/config/fsu1b.json

Failure mode:
  GCS unreachable → degrade to in-memory defaults. The gateway must
  still serve its admin surface so an operator can intervene.
  Save failures bubble up to the caller (PUT /admin/config) as a 502
  so the operator knows the change didn't persist.

NOT used for credentials. Credentials are Secret Manager only
(CHI-POL-003).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from core.config import apply_dict, get_settings, settings_to_dict

logger = logging.getLogger(__name__)


def _disabled() -> bool:
    """When set, all GCP I/O is skipped — used by tests and local dev."""
    return bool(os.environ.get("FSU1B_DISABLE_GCP_IO"))


def _bucket_and_blob():
    s = get_settings()
    return s.config_bucket, s.config_blob


def load_config_from_gcs() -> dict[str, Any]:
    """Hydrate settings from GCS. Returns the dict that was applied.

    If the blob is absent, write the current defaults and return them.
    If GCS is unreachable, log a warning and keep the in-memory defaults.
    """
    if _disabled():
        logger.info("FSU1B_DISABLE_GCP_IO set — skipping GCS config load.")
        return settings_to_dict()
    bucket_name, blob_name = _bucket_and_blob()
    try:
        from google.cloud import storage  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.warning("GCS client unavailable (%s) — using in-memory defaults.", exc)
        return settings_to_dict()

    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        if blob.exists():
            payload = json.loads(blob.download_as_text())
            apply_dict(payload)
            logger.info("FSU1B config loaded from gs://%s/%s", bucket_name, blob_name)
            return settings_to_dict()
        # Blob missing — write defaults.
        defaults = settings_to_dict()
        blob.upload_from_string(
            json.dumps(defaults, indent=2),
            content_type="application/json",
        )
        logger.info(
            "FSU1B config absent — wrote defaults to gs://%s/%s",
            bucket_name, blob_name,
        )
        return defaults
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "FSU1B config load failed (%s) — using in-memory defaults.", exc,
        )
        return settings_to_dict()


def save_config_to_gcs(payload: dict[str, Any]) -> bool:
    """Apply the payload to in-memory settings then persist to GCS.

    Returns True on persistence success, False otherwise. The caller
    decides what to do with a failure (PUT /admin/config returns 502).
    """
    # Apply first so the change takes effect even if GCS write fails;
    # the operator can retry the save later.
    apply_dict(payload)

    if _disabled():
        logger.info("FSU1B_DISABLE_GCP_IO set — skipping GCS config save.")
        return True

    bucket_name, blob_name = _bucket_and_blob()
    try:
        from google.cloud import storage  # type: ignore[import-not-found]

        client = storage.Client()
        blob = client.bucket(bucket_name).blob(blob_name)
        blob.upload_from_string(
            json.dumps(settings_to_dict(), indent=2),
            content_type="application/json",
        )
        logger.info("FSU1B config persisted to gs://%s/%s", bucket_name, blob_name)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("FSU1B config persistence failed: %s", exc)
        return False
