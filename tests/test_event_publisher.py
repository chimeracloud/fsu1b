"""Phase 4 — Pub/Sub event envelope publishing (Bible §20).

In tests we run in stub mode — no real Pub/Sub. The publisher logs
the envelope and broadcasts it to the SSE `all` channel; we verify
the envelope shape and the broadcast.
"""
import pytest

from core.config import reset_settings_for_test
from core.state import app_state, reset_state_for_test


@pytest.fixture(autouse=True)
def _isolated():
    reset_settings_for_test()
    reset_state_for_test()

    # Force the publisher into stub mode for every test.
    from services import event_publisher
    event_publisher.reset_publisher_for_test()
    event_publisher._publisher_disabled = True  # noqa: SLF001

    yield

    event_publisher.reset_publisher_for_test()
    reset_settings_for_test()
    reset_state_for_test()


@pytest.mark.asyncio
async def test_build_envelope_shape():
    from services.event_publisher import build_envelope

    env = build_envelope("gateway_started", {"foo": "bar"})
    assert set(env.keys()) == {"envelope", "payload"}
    assert env["envelope"]["source"] == "fsu1b"
    assert env["envelope"]["event_type"] == "gateway_started"
    assert env["envelope"]["version"] == "1.0"
    assert "timestamp" in env["envelope"]
    assert env["payload"] == {"foo": "bar"}


def test_build_envelope_rejects_unknown_type():
    from services.event_publisher import build_envelope

    with pytest.raises(ValueError):
        build_envelope("not_a_real_event", {})


@pytest.mark.asyncio
async def test_all_seven_event_types_accepted():
    from services.event_publisher import build_envelope, VALID_EVENT_TYPES

    expected = {
        "gateway_started",
        "gateway_stopped",
        "gateway_session_dropped",
        "gateway_session_recovered",
        "gateway_stream_stale",
        "gateway_reconnected",
        "gateway_daily_summary",
    }
    assert VALID_EVENT_TYPES == expected
    for et in expected:
        env = build_envelope(et, {})
        assert env["envelope"]["event_type"] == et


@pytest.mark.asyncio
async def test_publish_in_stub_mode_broadcasts_to_all_channel():
    from services.event_publisher import publish

    queue = await app_state.subscribe("all")
    try:
        env = await publish("gateway_started", {"phase": 4})
        assert env["envelope"]["event_type"] == "gateway_started"

        # The publish should have broadcast to the 'all' channel.
        msg = await queue.get()
        assert msg["event"] == "infra_event"
        assert msg["envelope"]["event_type"] == "gateway_started"
        assert msg["payload"] == {"phase": 4}
    finally:
        await app_state.unsubscribe("all", queue)


@pytest.mark.asyncio
async def test_publish_adds_activity_entry():
    from services.event_publisher import publish

    await publish("gateway_session_dropped", {"cause": "EOFError"})
    activity = app_state.recent_activity(limit=10)
    assert activity, "no activity recorded"
    latest = activity[-1]
    assert latest["kind"] == "event:gateway_session_dropped"


@pytest.mark.asyncio
async def test_publish_each_event_type_succeeds_in_stub_mode():
    """Smoke: every documented event type publishes without error."""
    from services.event_publisher import publish, VALID_EVENT_TYPES

    for et in VALID_EVENT_TYPES:
        env = await publish(et, {"smoke": True})
        assert env["envelope"]["event_type"] == et
