"""
Betfair authentication — async certlogin + keepalive.

Ported from FSU1A. Adapted to support the two-key model:

    await get_session_token(key="live")     # LIVE app key
    await get_session_token(key="delayed")  # DELAYED app key (Phase 3)

Each key has its own cached session token and acquisition timestamp,
because the two app keys are independent Betfair sessions.

Cert login is a synchronous `requests` call — dispatched via
`run_in_executor` so the asyncio loop is never blocked. Each key has
its own asyncio.Lock so concurrent stream reconnects don't race.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from core.secrets import get_credentials
from core.state import app_state

logger = logging.getLogger(__name__)
UTC = timezone.utc

KeyKind = Literal["live", "delayed"]

CERTLOGIN_URL = "https://identitysso-cert.betfair.com/api/certlogin"
KEEPALIVE_URL = "https://identitysso.betfair.com/api/keepAlive"


class _SessionStore:
    """Per-key session token cache."""

    def __init__(self) -> None:
        self.token: Optional[str] = None
        self.acquired_at: Optional[datetime] = None
        self.lock = asyncio.Lock()


_stores: dict[KeyKind, _SessionStore] = {"live": _SessionStore(), "delayed": _SessionStore()}


def _info_for(key: KeyKind):
    return app_state.live_session if key == "live" else app_state.delayed_session


async def get_session_token(key: KeyKind = "live") -> str:
    """Return a valid session token, refreshing via certlogin if stale."""
    store = _stores[key]
    async with store.lock:
        from core.config import get_settings

        settings = get_settings()
        keepalive_hours = settings.session_keepalive_hours

        if store.token and store.acquired_at:
            age = datetime.now(UTC) - store.acquired_at
            if age < timedelta(hours=keepalive_hours):
                return store.token

        token = await _do_certlogin(key)
        store.token = token
        store.acquired_at = datetime.now(UTC)
        info = _info_for(key)
        info.state = "active"
        info.last_login = store.acquired_at
        info.last_error = None
        return token


async def refresh_session(key: KeyKind = "live") -> str:
    """Force a fresh certlogin."""
    store = _stores[key]
    async with store.lock:
        store.token = None
        store.acquired_at = None
    return await get_session_token(key)


async def keepalive(key: KeyKind = "live") -> bool:
    """Extend the session lifetime."""
    store = _stores[key]
    info = _info_for(key)
    async with store.lock:
        if not store.token:
            return False
        creds = get_credentials(key)
        loop = asyncio.get_event_loop()
        try:
            ok = await loop.run_in_executor(
                None, _do_keepalive_sync, creds, store.token
            )
            if ok:
                store.acquired_at = datetime.now(UTC)
                info.last_keepalive = store.acquired_at
            else:
                store.token = None
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning("Keepalive failed for key=%s: %s", key, exc)
            store.token = None
            info.last_error = f"keepalive failed: {exc}"
            app_state.add_activity("keepalive_failed", f"key={key} error={exc}")
            return False


# ── Internals ────────────────────────────────────────────────────────────


async def _do_certlogin(key: KeyKind) -> str:
    info = _info_for(key)
    info.state = "logging_in"
    creds = get_credentials(key)
    loop = asyncio.get_event_loop()
    try:
        token = await loop.run_in_executor(None, _do_certlogin_sync, creds)
        return token
    except Exception as exc:
        info.state = "failed"
        info.last_error = f"certlogin failed: {exc}"
        app_state.add_activity("certlogin_failed", f"key={key} error={exc}")
        raise


def _do_certlogin_sync(creds: dict) -> str:
    import requests  # local to keep tests importable without network deps

    resp = requests.post(
        CERTLOGIN_URL,
        data={"username": creds["username"], "password": creds["password"]},
        cert=(creds["cert_path"], creds["key_path"]),
        headers={
            "X-Application": creds["app_key"],
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("loginStatus") != "SUCCESS":
        raise RuntimeError(f"certlogin not SUCCESS: {body.get('loginStatus')}")
    token = body.get("sessionToken")
    if not token:
        raise RuntimeError("certlogin returned no sessionToken")
    logger.info("Betfair certlogin succeeded (key=%s).", creds.get("key_kind"))
    return token


def _do_keepalive_sync(creds: dict, token: str) -> bool:
    import requests

    resp = requests.post(
        KEEPALIVE_URL,
        headers={"X-Application": creds["app_key"], "X-Authentication": token},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("status") == "SUCCESS"


def reset_sessions_for_test() -> None:
    for s in _stores.values():
        s.token = None
        s.acquired_at = None
