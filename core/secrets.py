"""
Secret Manager loader.

Loads Betfair credentials from GCP Secret Manager on demand. Cert and
key PEMs are written to module-lifetime temp files because the
`requests` library used for certlogin needs file paths, not strings.

Two app keys are supported (CHI-ADR-015..023, today's architecture):

  - "live"    → LIVE key, used for the Exchange Stream and order
                placement (wagering activity).
  - "delayed" → DELAYED key, used for read-only REST (account,
                cleared orders, catalogue) in Phase 3.

Each call to `get_credentials(key)` returns a dict of:
    username, password, app_key, cert_pem, key_pem,
    cert_path, key_path

Values are cached per-key (the username/password/cert/key are shared
across both keys; only the app_key differs).
"""
from __future__ import annotations

import logging
import tempfile
from threading import RLock
from typing import Literal

logger = logging.getLogger(__name__)

KeyKind = Literal["live", "delayed"]

# Module-level caches.
_lock = RLock()
_creds_cache: dict[KeyKind, dict[str, str]] = {}
_cert_path: str | None = None
_key_path: str | None = None
_shared: dict[str, str] | None = None  # username/password/cert/key — shared across keys.


def _read_secret(client, gcp_project: str, secret_id: str) -> str:
    name = f"projects/{gcp_project}/secrets/{secret_id}/versions/latest"
    return client.access_secret_version(name=name).payload.data.decode("utf-8")


def _ensure_shared(gcp_project: str, refs) -> dict[str, str]:
    """Load username/password/cert/key once. Returns the shared bundle."""
    global _shared, _cert_path, _key_path

    if _shared is not None:
        return _shared

    # Lazy-import so unit tests can run without google-cloud-secret-manager.
    from google.cloud import secretmanager  # type: ignore[import-not-found]

    client = secretmanager.SecretManagerServiceClient()

    username = _read_secret(client, gcp_project, refs.username)
    password = _read_secret(client, gcp_project, refs.password)
    cert_pem = _read_secret(client, gcp_project, refs.cert_pem)
    key_pem = _read_secret(client, gcp_project, refs.key_pem)

    # Write cert/key to module-lifetime temp files for `requests`.
    cf = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
    cf.write(cert_pem)
    cf.flush()
    _cert_path = cf.name

    kf = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
    kf.write(key_pem)
    kf.flush()
    _key_path = kf.name

    _shared = {
        "username": username,
        "password": password,
        "cert_pem": cert_pem,
        "key_pem": key_pem,
        "cert_path": _cert_path,
        "key_path": _key_path,
    }
    logger.info("FSU1B shared Betfair credentials loaded (cert/key written to temp).")
    return _shared


def get_credentials(key: KeyKind) -> dict[str, str]:
    """Return the credential bundle for `live` or `delayed`."""
    with _lock:
        if key in _creds_cache:
            return _creds_cache[key]

        from core.config import get_settings  # local import to avoid cycle on test

        settings = get_settings()
        shared = _ensure_shared(settings.gcp_project, settings.secrets)

        from google.cloud import secretmanager  # type: ignore[import-not-found]

        client = secretmanager.SecretManagerServiceClient()
        ref = (
            settings.secrets.live_app_key
            if key == "live"
            else settings.secrets.delayed_app_key
        )
        app_key = _read_secret(client, settings.gcp_project, ref)

        bundle = {**shared, "app_key": app_key, "key_kind": key}
        _creds_cache[key] = bundle
        logger.info("FSU1B credentials loaded for key=%s (secret=%s).", key, ref)
        return bundle


def reset_credentials_for_test() -> None:
    """Test-only — drop caches so tests don't leak Secret Manager calls."""
    global _shared, _cert_path, _key_path
    with _lock:
        _creds_cache.clear()
        _shared = None
        _cert_path = None
        _key_path = None
