"""Phase 2 — watchdog logic (no Betfair, no network)."""
from datetime import datetime, timedelta, timezone

import pytest

from core.state import app_state, reset_state_for_test
from core.config import get_settings


@pytest.fixture(autouse=True)
def _reset():
    reset_state_for_test()
    yield
    reset_state_for_test()


def test_stream_is_fresh_within_threshold():
    from core.state import app_state as st  # re-import singleton
    st.last_message_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    assert st.stream_is_fresh(60) is True


def test_stream_is_stale_beyond_threshold():
    from core.state import app_state as st
    st.last_message_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    assert st.stream_is_fresh(60) is False


def test_stream_age_s_none_when_no_message():
    from core.state import app_state as st
    st.last_message_at = None
    assert st.stream_age_s() is None


def test_default_watchdog_settings_match_sc_spec():
    s = get_settings()
    assert s.stream_check_interval_s == 30
    assert s.stream_stale_threshold_s == 60


@pytest.mark.asyncio
async def test_watchdog_does_nothing_when_session_idle():
    """If the session isn't running, the watchdog must not act."""
    from services.watchdog import run_watchdog

    class FakeSession:
        is_running = False
        force_called = False

        def force_disconnect(self, reason: str = "") -> None:
            self.force_called = True

    fake = FakeSession()

    import asyncio

    async def short_run():
        task = asyncio.create_task(run_watchdog(fake))
        # Give it a moment, then cancel.
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await short_run()
    assert fake.force_called is False
