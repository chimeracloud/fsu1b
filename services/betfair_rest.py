"""
betfairlightweight REST helper for both keys.

Two clients are managed:

  * LIVE-key client    — used for order placement (place / cancel /
                         replace). Keeps the LIVE key warm with
                         wagering activity.
  * DELAYED-key client — used for read-only REST (account funds,
                         account statement, current orders, cleared
                         orders, market catalogue).

Session-token sharing (SC go-ahead, locked):
  The bfl APIClient is instantiated WITHOUT calling its `.login()`.
  Instead, `services.betfair_auth.get_session_token(key)` is called
  and the returned token is assigned to `client.session_token`.
  Conclusion: one certlogin per key, shared between the stream
  (Phase 2) and the REST surface (this module).

DRY_RUN:
  When `Settings.dry_run` is True, write-side calls (place/cancel/
  replace) DO NOT touch Betfair. The gateway returns a simulated
  success envelope. Read-side calls (account/funds, cleared, etc.)
  are unaffected.

Tape-recorder discipline (architecture doc §4.1, §8):
  The REST helper returns the betfairlightweight response unchanged,
  wrapped only with `{ok, latency_ms, gateway_ts, dry_run, betfair}`.
  No calculation, no enrichment.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Literal

from core.config import get_settings
from core.secrets import get_credentials
from core.state import app_state
from services.betfair_auth import get_session_token, refresh_session

logger = logging.getLogger(__name__)

KeyKind = Literal["live", "delayed"]

# Client cache. bfl APIClient is thread-safe for our usage: we only
# mutate `session_token` (atomic write to an attribute) and call its
# read/write methods, which serialise via the underlying `requests`
# session.
_lock = RLock()
_clients: dict[KeyKind, Any] = {}


def _make_client(key: KeyKind):
    """Build a betfairlightweight APIClient. Lazy-import keeps tests light."""
    import betfairlightweight as bfl  # type: ignore[import-not-found]

    creds = get_credentials(key)
    client = bfl.APIClient(
        username=creds["username"],
        password=creds["password"],
        app_key=creds["app_key"],
        certs=creds["certs_dir"],
        locale="uk",
    )
    return client


async def _get_client(key: KeyKind):
    """Return a ready-to-use bfl client with a fresh session token."""
    with _lock:
        client = _clients.get(key)
        if client is None:
            client = _make_client(key)
            _clients[key] = client

    # Fetch (or reuse cached) session token from betfair_auth and inject.
    try:
        token = await get_session_token(key)
    except Exception:
        token = await refresh_session(key)
    client.session_token = token
    return client


async def _run_in_executor(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def reset_clients_for_test() -> None:
    """Test-only — drop the cached bfl clients."""
    with _lock:
        _clients.clear()


# ─────────────────────────────────────────────────────────────────────────────
# DELAYED-key reads
# ─────────────────────────────────────────────────────────────────────────────


async def list_current_orders(**kwargs) -> dict:
    client = await _get_client("delayed")
    t0 = time.monotonic()
    result = await _run_in_executor(client.betting.list_current_orders, **kwargs)
    return _envelope(t0, result)


async def list_cleared_orders(**kwargs) -> dict:
    client = await _get_client("delayed")
    t0 = time.monotonic()
    result = await _run_in_executor(client.betting.list_cleared_orders, **kwargs)
    return _envelope(t0, result)


async def get_account_funds(**kwargs) -> dict:
    client = await _get_client("delayed")
    t0 = time.monotonic()
    result = await _run_in_executor(client.account.get_account_funds, **kwargs)
    return _envelope(t0, result)


async def get_account_statement(**kwargs) -> dict:
    client = await _get_client("delayed")
    t0 = time.monotonic()
    result = await _run_in_executor(client.account.get_account_statement, **kwargs)
    return _envelope(t0, result)


async def list_market_catalogue(**kwargs) -> dict:
    client = await _get_client("delayed")
    t0 = time.monotonic()
    result = await _run_in_executor(client.betting.list_market_catalogue, **kwargs)
    return _envelope(t0, result)


# ─────────────────────────────────────────────────────────────────────────────
# LIVE-key writes (order placement, wagering activity)
# ─────────────────────────────────────────────────────────────────────────────


async def place_orders(payload: dict) -> dict:
    settings = get_settings()
    if settings.dry_run:
        return _dry_run_place_response(payload)

    client = await _get_client("live")
    t0 = time.monotonic()
    try:
        result = await _run_in_executor(client.betting.place_orders, **payload)
    except Exception as exc:  # noqa: BLE001
        app_state.add_activity("order_place_error", repr(exc))
        raise

    env = _envelope(t0, result)
    app_state.add_activity(
        "order_placed",
        f"market_id={payload.get('market_id')} "
        f"instructions={len(payload.get('instructions', []))} "
        f"latency_ms={env['latency_ms']:.1f}",
    )
    return env


async def cancel_orders(payload: dict) -> dict:
    settings = get_settings()
    if settings.dry_run:
        return _dry_run_cancel_response(payload)

    client = await _get_client("live")
    t0 = time.monotonic()
    result = await _run_in_executor(client.betting.cancel_orders, **payload)
    env = _envelope(t0, result)
    app_state.add_activity("order_cancelled", f"market_id={payload.get('market_id')}")
    return env


async def replace_orders(payload: dict) -> dict:
    settings = get_settings()
    if settings.dry_run:
        return _dry_run_replace_response(payload)

    client = await _get_client("live")
    t0 = time.monotonic()
    result = await _run_in_executor(client.betting.replace_orders, **payload)
    env = _envelope(t0, result)
    app_state.add_activity("order_replaced", f"market_id={payload.get('market_id')}")
    return env


# ─────────────────────────────────────────────────────────────────────────────
# Envelope + DRY_RUN simulator
# ─────────────────────────────────────────────────────────────────────────────


def _envelope(t0_monotonic: float, result: Any) -> dict:
    """Wrap a betfairlightweight response into the standard gateway envelope.

    `result` from bfl is typically a resource object (e.g.
    `PlaceOrders`, `AccountFundsResponse`). We serialise via the
    `_data` attribute when present, else fall through to a generic dict
    coercion. The gateway does NOT interpret — Betfair's shape is
    preserved, only wrapped.
    """
    latency_ms = (time.monotonic() - t0_monotonic) * 1000.0
    return {
        "ok": True,
        "dry_run": False,
        "gateway_ts": _now(),
        "latency_ms": round(latency_ms, 3),
        "betfair": _serialise(result),
    }


def _serialise(result: Any) -> Any:
    """Best-effort conversion of a bfl resource to a JSON-safe dict."""
    if result is None:
        return None
    # bfl resources stash raw JSON in `_data`.
    raw = getattr(result, "_data", None)
    if raw is not None:
        return raw
    if isinstance(result, list):
        return [_serialise(x) for x in result]
    if hasattr(result, "json") and callable(result.json):
        try:
            return result.json()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(result, "__dict__"):
        return {k: v for k, v in result.__dict__.items() if not k.startswith("_")}
    return str(result)


def _dry_run_place_response(payload: dict) -> dict:
    """Simulate a Betfair `placeOrders` SUCCESS for every instruction."""
    instructions = payload.get("instructions", [])
    reports = []
    for i, ins in enumerate(instructions):
        reports.append(
            {
                "status": "SUCCESS",
                "instruction": ins,
                "betId": f"DRY-{int(time.time())}-{i}",
                "placedDate": _now().isoformat(),
                "averagePriceMatched": 0.0,
                "sizeMatched": 0.0,
                "orderStatus": "EXECUTABLE",
            }
        )
    logger.info(
        "[DRY_RUN] place_orders market_id=%s instructions=%d (no Betfair call)",
        payload.get("market_id"), len(instructions),
    )
    app_state.add_activity(
        "dry_run_place",
        f"market_id={payload.get('market_id')} instructions={len(instructions)}",
    )
    return {
        "ok": True,
        "dry_run": True,
        "gateway_ts": _now(),
        "latency_ms": 0.0,
        "betfair": {
            "status": "SUCCESS",
            "marketId": payload.get("market_id"),
            "customerRef": payload.get("customer_ref"),
            "customerStrategyRef": payload.get("customer_strategy_ref"),
            "instructionReports": reports,
        },
    }


def _dry_run_cancel_response(payload: dict) -> dict:
    instructions = payload.get("instructions", [])
    logger.info(
        "[DRY_RUN] cancel_orders market_id=%s instructions=%d (no Betfair call)",
        payload.get("market_id"), len(instructions),
    )
    app_state.add_activity(
        "dry_run_cancel",
        f"market_id={payload.get('market_id')} instructions={len(instructions)}",
    )
    return {
        "ok": True,
        "dry_run": True,
        "gateway_ts": _now(),
        "latency_ms": 0.0,
        "betfair": {
            "status": "SUCCESS",
            "marketId": payload.get("market_id"),
            "customerRef": payload.get("customer_ref"),
            "instructionReports": [
                {"status": "SUCCESS", "instruction": ins} for ins in instructions
            ],
        },
    }


def _dry_run_replace_response(payload: dict) -> dict:
    instructions = payload.get("instructions", [])
    logger.info(
        "[DRY_RUN] replace_orders market_id=%s instructions=%d (no Betfair call)",
        payload.get("market_id"), len(instructions),
    )
    app_state.add_activity(
        "dry_run_replace",
        f"market_id={payload.get('market_id')} instructions={len(instructions)}",
    )
    return {
        "ok": True,
        "dry_run": True,
        "gateway_ts": _now(),
        "latency_ms": 0.0,
        "betfair": {
            "status": "SUCCESS",
            "marketId": payload.get("market_id"),
            "customerRef": payload.get("customer_ref"),
            "instructionReports": [
                {
                    "status": "SUCCESS",
                    "cancelInstructionReport": {"status": "SUCCESS", "instruction": {
                        "betId": ins.get("bet_id"),
                    }},
                    "placeInstructionReport": {
                        "status": "SUCCESS",
                        "betId": f"DRY-REPL-{ins.get('bet_id')}",
                        "placedDate": _now().isoformat(),
                    },
                }
                for ins in instructions
            ],
        },
    }
