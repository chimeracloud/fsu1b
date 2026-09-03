"""
Subscription-limit tests.

Covers the gap closed on 2026-09-03: the ceiling is a GCS-persisted
Setting defaulting to Chimera's granted 1,000-market allocation
(Betfair request 56184, granted 2026-06-08), the live count is exposed
on /admin/status, and the gateway warns at 90% instead of waiting for
Betfair to drop the stream with SUBSCRIPTION_LIMIT_EXCEEDED.
"""
import asyncio

import pytest

from core.config import get_settings, replace_settings, reset_settings_for_test
from core.state import app_state, reset_state_for_test
from services import subscription_guard


@pytest.fixture(autouse=True)
def _clean_settings():
    # The activity deque is a module-level singleton; without clearing
    # it, assertions here would read events left by earlier tests.
    reset_settings_for_test()
    reset_state_for_test()
    yield
    reset_settings_for_test()
    reset_state_for_test()


# ── the limit is a setting, not a constant ───────────────────────────


def test_default_limit_is_the_granted_thousand():
    assert get_settings().subscription_limit == 1000
    assert get_settings().subscription_warn_pct == 0.9


def test_limit_is_readable_on_admin_config(client):
    body = client.get("/admin/config").json()
    assert body["subscription_limit"] == 1000
    assert body["subscription_warn_pct"] == 0.9


def test_limit_is_editable_via_put_and_persists(client):
    body = client.put("/admin/config", json={"subscription_limit": 1500}).json()
    assert body["subscription_limit"] == 1500
    # Survives into the settings object the rest of the app reads.
    assert get_settings().subscription_limit == 1500
    assert client.get("/admin/config").json()["subscription_limit"] == 1500


def test_limit_round_trips_through_the_gcs_payload():
    """It must serialise, or it would never reach the GCS blob."""
    from core.config import apply_dict, settings_to_dict

    replace_settings(subscription_limit=750)
    payload = settings_to_dict()
    assert payload["subscription_limit"] == 750

    reset_settings_for_test()
    assert get_settings().subscription_limit == 1000
    apply_dict(payload)
    assert get_settings().subscription_limit == 750


def test_limit_is_not_read_from_the_environment(monkeypatch):
    """CHI-POL-006 — env vars carry deploy-time identity, never settings."""
    monkeypatch.setenv("SUBSCRIPTION_LIMIT", "42")
    monkeypatch.setenv("FSU1B_SUBSCRIPTION_LIMIT", "42")
    reset_settings_for_test()
    assert get_settings().subscription_limit == 1000


# ── the live count is exposed ────────────────────────────────────────


def test_admin_status_exposes_count_against_limit(client):
    subs = client.get("/admin/status").json()["subscriptions"]
    assert subs["market_count"] == 0
    assert subs["limit"] == 1000
    assert subs["warn_at"] == 900
    assert subs["pct_of_limit"] == 0.0
    assert subs["at_warning_level"] is False


def test_warn_at_tracks_a_changed_limit(client):
    client.put("/admin/config", json={"subscription_limit": 200})
    subs = client.get("/admin/status").json()["subscriptions"]
    assert subs["limit"] == 200
    assert subs["warn_at"] == 180


# ── warning behaviour ────────────────────────────────────────────────


def test_level_reports_warning_at_ninety_percent():
    assert subscription_guard.level(899)["at_warning_level"] is False
    assert subscription_guard.level(900)["at_warning_level"] is True
    assert subscription_guard.level(900)["pct_of_limit"] == 0.9


def test_warns_once_when_crossing_the_threshold():
    fired = asyncio.run(subscription_guard.check(900, force=True))
    assert fired is True

    kinds = [e["kind"] for e in app_state.recent_activity(limit=5)]
    assert "subscription_warning" in kinds

    # Latched — a second call at the same level must not re-warn.
    assert asyncio.run(subscription_guard.check(950, force=True)) is False


def test_does_not_warn_below_the_threshold():
    assert asyncio.run(subscription_guard.check(899, force=True)) is False
    kinds = [e["kind"] for e in app_state.recent_activity(limit=5)]
    assert "subscription_warning" not in kinds


def test_warning_clears_with_hysteresis_then_can_fire_again():
    assert asyncio.run(subscription_guard.check(900, force=True)) is True

    # Just under warn_at is inside the hysteresis band — stays latched.
    assert asyncio.run(subscription_guard.check(880, force=True)) is False

    # Below warn_at - 5% of limit (900 - 50 = 850) clears the latch.
    assert asyncio.run(subscription_guard.check(840, force=True)) is False
    kinds = [e["kind"] for e in app_state.recent_activity(limit=5)]
    assert "subscription_level_normal" in kinds

    # Cleared, so a fresh climb warns again.
    assert asyncio.run(subscription_guard.check(900, force=True)) is True


def test_warning_threshold_follows_the_configured_limit():
    """At the old 200 ceiling, Phase 5's observed 198 markets must warn."""
    replace_settings(subscription_limit=200)
    assert subscription_guard.level(198)["at_warning_level"] is True
    assert asyncio.run(subscription_guard.check(198, force=True)) is True


def test_thousand_markets_is_no_longer_a_warning():
    """The whole point of the increase: 198 markets is now unremarkable."""
    assert subscription_guard.level(198)["at_warning_level"] is False
    assert asyncio.run(subscription_guard.check(198, force=True)) is False


def test_check_is_throttled_between_calls():
    """Unforced calls must not re-count on every message."""
    assert asyncio.run(subscription_guard.check(900)) is True
    # Immediately after, the throttle window suppresses the next check.
    assert asyncio.run(subscription_guard.check(900)) is False


def test_due_gates_the_hot_path_before_counting():
    """The stream loop asks due() first so it can skip counting entirely."""
    assert subscription_guard.due() is True
    asyncio.run(subscription_guard.check(10))
    assert subscription_guard.due() is False
    subscription_guard.reset_for_test()
    assert subscription_guard.due() is True


def test_zero_limit_does_not_divide_by_zero():
    replace_settings(subscription_limit=0)
    info = subscription_guard.level(10)
    assert info["at_warning_level"] is True
    assert info["pct_of_limit"] > 0


# ── the ceiling is legible when Betfair rejects the subscription ─────


def test_subscription_limit_exceeded_is_recorded_at_subscribe_time():
    from services import stream_client

    with pytest.raises(RuntimeError):
        stream_client._check_status(
            {
                "statusCode": "FAILURE",
                "errorCode": "SUBSCRIPTION_LIMIT_EXCEEDED",
                "errorMessage": "too many markets",
            },
            "marketSubscription",
        )

    events = app_state.recent_activity(limit=5)
    detail = next(
        e["detail"] for e in events if e["kind"] == "subscription_limit_exceeded"
    )
    # The operator must be able to see it is the filter, not the network.
    assert "subscription_limit=1000" in detail
    assert "PUT /admin/config" in detail


def test_other_subscribe_failures_are_not_mislabelled():
    from services import stream_client

    with pytest.raises(RuntimeError):
        stream_client._check_status(
            {"statusCode": "FAILURE", "errorCode": "INVALID_SESSION_INFORMATION"},
            "marketSubscription",
        )
    kinds = [e["kind"] for e in app_state.recent_activity(limit=5)]
    assert "subscription_limit_exceeded" not in kinds
