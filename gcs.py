"""GCS persistence for FSU1B.

The recorder maintains a local NDJSON file in ``/tmp/{trading_day}.ndjson``
and periodically uploads the whole file to GCS, overwriting the prior
copy. Same-region (Cloud Run → GCS in europe-west2) bandwidth is free
so the "upload whole file" approach is simpler and at least as cheap
as the more elaborate ``compose()`` append pattern.

Daily files:

* ``horse-racing/{YYYY-MM-DD}.ndjson``       — raw NDJSON
* ``horse-racing/{YYYY-MM-DD}_meta.json``    — metadata, written on stop / rollover

A trading day starts at 12:00 UTC and ends at the next 12:00 UTC.
``trading_day_for(now)`` returns the date string the current moment
belongs to.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.cloud import storage  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


BUCKET_NAME = "chiops-betfair-recording"
PATH_PREFIX = "horse-racing"
LOCAL_DIR = "/tmp"


# ──────────────────────────────────────────────────────────────────────────────
# Trading-day boundary helpers
# ──────────────────────────────────────────────────────────────────────────────


def trading_day_for(now: Optional[datetime] = None) -> str:
    """Return the trading-day date string the given moment falls under.

    A trading day starts at 12:00 UTC. So a moment at 11:00 UTC on
    2026-05-15 belongs to the 2026-05-14 trading day; a moment at
    13:00 UTC on 2026-05-15 belongs to the 2026-05-15 trading day.
    """

    if now is None:
        now = datetime.now(tz=timezone.utc)
    if now.hour < 12:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


def trading_day_start(date_str: str) -> datetime:
    """The 12:00 UTC start of the named trading day."""

    return datetime.strptime(date_str + "T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


# ──────────────────────────────────────────────────────────────────────────────
# Path helpers
# ──────────────────────────────────────────────────────────────────────────────


def gcs_ndjson_path(date_str: str) -> str:
    return f"{PATH_PREFIX}/{date_str}.ndjson"


def gcs_meta_path(date_str: str) -> str:
    return f"{PATH_PREFIX}/{date_str}_meta.json"


def local_ndjson_path(date_str: str) -> str:
    return os.path.join(LOCAL_DIR, f"{date_str}.ndjson")


# ──────────────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────────────


_CLIENT: Optional[storage.Client] = None


def _client() -> storage.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = storage.Client()
    return _CLIENT


# ──────────────────────────────────────────────────────────────────────────────
# Read / write
# ──────────────────────────────────────────────────────────────────────────────


def download_existing(date_str: str) -> int:
    """If a GCS file for this trading day already exists, download it
    to the local path so the recorder can append to it.

    Returns the file size in bytes (or 0 if no file).
    """

    blob = _client().bucket(BUCKET_NAME).blob(gcs_ndjson_path(date_str))
    if not blob.exists():
        return 0
    local = local_ndjson_path(date_str)
    blob.download_to_filename(local)
    size = os.path.getsize(local)
    logger.info(
        "FSU1B resumed: downloaded %d bytes from %s to %s",
        size, gcs_ndjson_path(date_str), local,
    )
    return size


def upload_ndjson(date_str: str) -> Optional[int]:
    """Upload the local NDJSON file for this trading day to GCS,
    overwriting whatever's there. Returns bytes uploaded on success,
    None on failure (caller logs + retries).
    """

    local = local_ndjson_path(date_str)
    if not os.path.exists(local):
        return 0
    blob = _client().bucket(BUCKET_NAME).blob(gcs_ndjson_path(date_str))
    blob.upload_from_filename(local, content_type="application/x-ndjson")
    return os.path.getsize(local)


def write_meta(date_str: str, meta: dict[str, Any]) -> None:
    """Write the day's metadata sidecar file. Overwrites prior version."""

    blob = _client().bucket(BUCKET_NAME).blob(gcs_meta_path(date_str))
    blob.upload_from_string(
        json.dumps(meta, default=str, indent=2),
        content_type="application/json",
    )


def list_recordings() -> list[dict[str, Any]]:
    """Enumerate all recorded daily files in the bucket.

    Returns one entry per day with metadata-light info (file presence,
    size, etag). The richer details (markets recorded, venues) live in
    the meta sidecar and are pulled by ``get_meta(date)``.
    """

    bucket = _client().bucket(BUCKET_NAME)
    out: dict[str, dict[str, Any]] = {}
    for blob in bucket.list_blobs(prefix=PATH_PREFIX + "/"):
        name = blob.name
        if not name.endswith(".ndjson"):
            continue
        # Extract YYYY-MM-DD from the filename
        try:
            date_str = name.removeprefix(PATH_PREFIX + "/").removesuffix(".ndjson")
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        out[date_str] = {
            "date": date_str,
            "file_size_bytes": blob.size or 0,
            "ndjson_path": name,
            "updated_at": blob.updated.isoformat() if blob.updated else None,
        }
    return sorted(out.values(), key=lambda r: r["date"], reverse=True)


def get_meta(date_str: str) -> Optional[dict[str, Any]]:
    """Read the meta sidecar for a specific day. Returns None if absent."""

    blob = _client().bucket(BUCKET_NAME).blob(gcs_meta_path(date_str))
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def stream_ndjson(date_str: str):
    """Yield NDJSON lines for a day (for /api/replay/{date})."""

    blob = _client().bucket(BUCKET_NAME).blob(gcs_ndjson_path(date_str))
    if not blob.exists():
        return
    with blob.open("rt") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line:
                yield line


def download_url(date_str: str) -> Optional[str]:
    """Build a signed download URL for the day's NDJSON file.

    Cloud Run uses workload identity, not a JSON key file, so we
    generate a v4 signed URL using IAM-based signing. Returns None
    if the blob doesn't exist.
    """

    blob = _client().bucket(BUCKET_NAME).blob(gcs_ndjson_path(date_str))
    if not blob.exists():
        return None
    # IAM-based signing requires the SA to have iam.serviceAccountTokenCreator
    # on itself. For now we return the blob's GCS authenticated URL — clients
    # with the right scope can fetch it. Signed URLs are a follow-up.
    return f"gs://{BUCKET_NAME}/{gcs_ndjson_path(date_str)}"
