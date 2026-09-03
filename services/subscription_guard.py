"""
Stream subscription-level guard.

Betfair caps how many markets a single stream subscription may hold.
Chimera's allocation was raised 200 -> 1,000 markets per session on
2026-06-08 (Betfair request 56184, application id 137035, application
name `intakehub`).

The ceiling is not a soft cap. Betfair answers the marketSubscription
with `SUBSCRIPTION_LIMIT_EXCEEDED`, which fails the whole subscription
and drops the stream — the supervisor then retries the identical
filter, gets the identical error, and backs off. That reconnect storm
is exactly what the allocation increase was requested to prevent, so
this module warns on the *approach* instead of waiting for the break.

The limit lives in `Settings.subscription_limit`, persisted to GCS and
editable through `PUT /admin/config` (CHI-POL-006) — never an env var,
so a future allocation change needs no redeploy.

Counting: the live market cache is the best available proxy for
"markets this subscription is carrying". It can briefly overstate,
because CLOSED markets stay cached until the 300s maintenance sweep
evicts them.
"""
from __future__ import annotations

import logging
from time import monotonic

from core.config import get_settings
from core.state import app_state

logger = logging.getLogger(__name__)

# Don't re-count on every MCM. At 1,000 markets the stream can carry
# hundreds of messages a second; once every few seconds is ample for a
# population that only grows as markets open.
_CHECK_INTERVAL_S = 5.0

# Clear the warning a little below the trigger so a count hovering on
# the threshold doesn't flap the log and the event topic.
_HYSTERESIS_PCT = 0.05

_last_check: float = 0.0
_warned: bool = False


def reset_for_test() -> None:
    """Test-only — drop the throttle and latch."""
    global _last_check, _warned
    _last_check = 0.0
    _warned = False


def due() -> bool:
    """True when the throttle window has elapsed.

    Lets callers on the hot path skip counting the cache at all, rather
    than counting it and having `check` discard the result.
    """
    return (monotonic() - _last_check) >= _CHECK_INTERVAL_S


def level(market_count: int) -> dict:
    """Describe the subscription level. Pure — safe for /admin/status."""
    settings = get_settings()
    limit = max(1, settings.subscription_limit)
    warn_at = int(limit * settings.subscription_warn_pct)
    return {
        "limit": settings.subscription_limit,
        "warn_at": warn_at,
        "pct_of_limit": round(market_count / limit, 4),
        "at_warning_level": market_count >= warn_at,
    }


async def check(market_count: int, *, force: bool = False) -> bool:
    """Warn when the live count reaches `subscription_warn_pct` of the limit.

    Returns True when a warning fired on this call. Throttled to one
    count check per `_CHECK_INTERVAL_S` unless `force` is set.
    """
    global _last_check, _warned

    now = monotonic()
    if not force and (now - _last_check) < _CHECK_INTERVAL_S:
        return False
    _last_check = now

    info = level(market_count)
    limit = info["limit"]
    warn_at = info["warn_at"]

    # Below the clear-threshold — drop the latch so a later climb warns again.
    if _warned and market_count < warn_at - int(limit * _HYSTERESIS_PCT):
        _warned = False
        logger.info(
            "Subscription level back to normal: %d/%d markets.", market_count, limit,
        )
        app_state.add_activity(
            "subscription_level_normal", f"{market_count}/{limit} markets",
        )
        return False

    if not info["at_warning_level"] or _warned:
        return False

    _warned = True
    detail = (
        f"{market_count}/{limit} markets "
        f"({info['pct_of_limit'] * 100:.0f}% of the configured limit)"
    )
    logger.warning(
        "Subscription approaching Betfair's ceiling: %s. "
        "SUBSCRIPTION_LIMIT_EXCEEDED would drop the whole stream — "
        "narrow the market filter or raise subscription_limit if the "
        "Betfair allocation has grown.",
        detail,
    )
    app_state.add_activity("subscription_warning", detail)

    # Local import: keeps event_publisher off the import graph for tests
    # that never touch Pub/Sub.
    from services.event_publisher import publish

    try:
        await publish(
            "gateway_subscription_warning",
            {
                "market_count": market_count,
                "limit": limit,
                "warn_at": warn_at,
                "pct_of_limit": info["pct_of_limit"],
            },
        )
    except Exception as exc:  # noqa: BLE001 — never derail the stream loop
        logger.warning("subscription_warning publish failed: %s", exc)

    return True
