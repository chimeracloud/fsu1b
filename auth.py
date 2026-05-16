"""Betfair client builder — same pattern as CLE V2, isolated copy.

FSU1B deliberately does not import from fsu100. The two services share
the same Betfair credentials in Secret Manager but maintain independent
stream subscriptions. A bug in one service's auth path must not
propagate to the other.

Credentials live in Secret Manager (project ``chiops``) at:

* ``betfair-username``
* ``betfair-password``
* ``betfair-app-key``
* ``betfair-cert-pem``  — cert as PEM string
* ``betfair-key-pem``   — private key as PEM string

The fsu1b-sa service account has ``secretmanager.secretAccessor`` on
each of the five secrets.
"""

from __future__ import annotations

import logging
import os
import tempfile

import betfairlightweight  # type: ignore[import-untyped]
from google.cloud import secretmanager  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GCP_PROJECT", "chiops")


def _read_secret(secret_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


def build_trading_client() -> betfairlightweight.APIClient:
    """Construct + log in to a betfairlightweight APIClient.

    Cert + key are written to a fresh per-process temp dir on disk
    because betfairlightweight's underlying ``requests`` library
    expects file paths, not in-memory bytes.
    """

    username = _read_secret("betfair-username")
    password = _read_secret("betfair-password")
    app_key = _read_secret("betfair-app-key")
    cert_pem = _read_secret("betfair-cert-pem")
    key_pem = _read_secret("betfair-key-pem")

    certs_dir = tempfile.mkdtemp(prefix="fsu1b-certs-")
    cert_path = os.path.join(certs_dir, "client-2048.crt")
    key_path = os.path.join(certs_dir, "client-2048.key")
    with open(cert_path, "w") as f:
        f.write(cert_pem)
    with open(key_path, "w") as f:
        f.write(key_pem)
    os.chmod(cert_path, 0o600)
    os.chmod(key_path, 0o600)

    client = betfairlightweight.APIClient(
        username=username,
        password=password,
        app_key=app_key,
        certs=certs_dir,
        locale="uk",
    )
    client.login()
    logger.info("FSU1B Betfair client logged in successfully")
    return client
