"""
Stream supervisor: reconnect backoff and post-stop honesty.

Two defects found in the 2026-09-03 audit:

  * `_stream_supervisor` initialised `backoff = 1` once and then
    doubled it on every loop iteration for the life of the session,
    never resetting after a connection that actually came up. The
    backoff therefore measured how many times the session had EVER
    reconnected, not how badly the current reconnect was going — after
    ~9 reconnects it pinned at reconnect_max_backoff_s (300s), so a
    drop late in a soak cost five minutes of missed market data where
    the first drop cost one second.

  * `stop()` closed the TCP connection but left `live_session.state`
    reading "active", because betfair_auth had set it at certlogin and
    nothing since had reason to revise it. A stopped gateway claiming
    an active LIVE session is how the next incident gets misdiagnosed.
"""
import asyncio

import pytest

from core.config import replace_settings, reset_settings_for_test
from core.state import app_state, reset_state_for_test
from services import stream_client
from services.stream_session import StreamSession


@pytest.fixture(autouse=True)
def _clean():
    reset_settings_for_test()
    reset_state_for_test()
    yield
    reset_settings_for_test()
    reset_state_for_test()


async def _drive(session, monkeypatch, *, attempts, connect):
    """Run the supervisor for `attempts` connection attempts.

    `connect(n)` decides whether attempt n reaches 'connected' before
    the connection dies. Returns the list of backoff waits the
    supervisor asked for, in order.
    """
    waits: list[float] = []
    calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_run_connection():
        n = calls["n"]
        calls["n"] += 1
        if connect(n):
            # run_connection sets this once the subscription is
            # acknowledged; the announcer task watches for it.
            app_state.stream_status = "connected"
            # Yield enough times for the announcer to observe it.
            for _ in range(3):
                await real_sleep(0)
        raise RuntimeError(f"connection {n} died")

    async def fake_sleep(delay, *a, **kw):
        # The announcer polls on 0.5s; everything else on this module's
        # path is the supervisor's backoff.
        if delay != 0.5:
            waits.append(delay)
            if len(waits) >= attempts:
                session._running = False
        await real_sleep(0)

    monkeypatch.setattr(stream_client, "run_connection", fake_run_connection)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    try:
        await asyncio.wait_for(session._stream_supervisor(), timeout=5)
    finally:
        monkeypatch.setattr(asyncio, "sleep", real_sleep)
    return waits


# ── the defect ───────────────────────────────────────────────────────


async def test_backoff_resets_after_every_successful_connection(monkeypatch):
    """Every attempt comes up, so every backoff must be the 1s floor."""
    session = StreamSession()
    session._running = True

    waits = await _drive(
        session, monkeypatch, attempts=6, connect=lambda n: True,
    )

    assert waits == [1, 1, 1, 1, 1, 1], (
        f"backoff grew across successful connections: {waits}"
    )


async def test_backoff_still_grows_while_connections_never_come_up(monkeypatch):
    """A genuinely broken endpoint must still back off exponentially."""
    session = StreamSession()
    session._running = True

    waits = await _drive(
        session, monkeypatch, attempts=6, connect=lambda n: False,
    )

    assert waits == [1, 2, 4, 8, 16, 32], f"backoff did not grow: {waits}"


async def test_backoff_recovers_after_a_bad_patch(monkeypatch):
    """The case the defect broke: grow while failing, reset once healthy.

    Before the fix this ended [1, 2, 4, 8, 16, 32] — the successful
    connections at attempts 4 and 5 changed nothing.
    """
    session = StreamSession()
    session._running = True

    # Attempts 0-3 fail, 4 and 5 come up.
    waits = await _drive(
        session, monkeypatch, attempts=6, connect=lambda n: n >= 4,
    )

    assert waits[:4] == [1, 2, 4, 8], f"growth phase wrong: {waits}"
    assert waits[4] == 1, f"backoff did not reset after success: {waits}"
    assert waits[5] == 1, f"backoff did not stay reset: {waits}"


async def test_backoff_is_capped_by_the_configured_maximum(monkeypatch):
    replace_settings(reconnect_max_backoff_s=5)
    session = StreamSession()
    session._running = True

    waits = await _drive(
        session, monkeypatch, attempts=6, connect=lambda n: False,
    )

    assert max(waits) <= 5, f"backoff exceeded the cap: {waits}"


async def test_a_connection_that_never_came_up_does_not_reset_backoff(monkeypatch):
    """Flapping — dying before 'connected' — must keep backing off."""
    session = StreamSession()
    session._running = True

    waits = await _drive(
        session, monkeypatch, attempts=4, connect=lambda n: False,
    )

    assert waits == [1, 2, 4, 8]
    assert session._conn_established is False


# ── stop() honesty ───────────────────────────────────────────────────


async def test_stop_reports_the_session_as_stopped_not_active():
    session = StreamSession()
    session._running = True
    session._tasks = []

    # certlogin leaves the session reading 'active'.
    app_state.live_session.state = "active"

    result = await session.stop()

    assert result["accepted"] is True
    assert app_state.live_session.state == "stopped"
    assert app_state.stream_status == "disconnected"


async def test_stop_does_not_touch_the_delayed_session():
    """DELAYED is a separate key — stopping the stream must not mark it."""
    session = StreamSession()
    session._running = True
    session._tasks = []

    app_state.live_session.state = "active"
    app_state.delayed_session.state = "active"

    await session.stop()

    assert app_state.live_session.state == "stopped"
    assert app_state.delayed_session.state == "active"


def test_admin_status_shows_stopped_and_what_is_still_held(client):
    """The operator must be able to tell 'no stream' from 'no credential'."""
    app_state.live_session.state = "stopped"

    body = client.get("/admin/status").json()
    assert body["live_session"]["state"] == "stopped"
    # No certlogin has run in tests, so nothing is held.
    assert body["live_session"]["token_cached"] is False
    assert body["delayed_session"]["token_cached"] is False


def test_token_cached_reflects_a_held_credential(client):
    from services.betfair_auth import _stores, reset_sessions_for_test

    _stores["live"].token = "fake-session-token"  # noqa: S105 — test double
    try:
        body = client.get("/admin/status").json()
        assert body["live_session"]["token_cached"] is True
        assert body["delayed_session"]["token_cached"] is False
    finally:
        reset_sessions_for_test()
