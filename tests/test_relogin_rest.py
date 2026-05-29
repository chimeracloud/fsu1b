"""
Phase 3 — `POST /admin/control/relogin_rest`.

Forces a fresh DELAYED-key certlogin and drops the cached bfl client.
We patch `refresh_session` so no Betfair call is made and the
response shape is exercised.
"""
import pytest

from core.config import reset_settings_for_test
from core.state import reset_state_for_test


@pytest.fixture(autouse=True)
def _isolated():
    reset_settings_for_test()
    reset_state_for_test()
    yield
    reset_settings_for_test()
    reset_state_for_test()


def test_relogin_rest_success(client, monkeypatch):
    calls = []

    async def fake_refresh(key="live"):
        calls.append(key)
        return "fake-token"

    from services import betfair_auth, betfair_rest
    monkeypatch.setattr(betfair_auth, "refresh_session", fake_refresh)

    r = client.post("/admin/control/relogin_rest")
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["executed"] is True
    assert calls == ["delayed"]


def test_relogin_rest_failure_reports_unexecuted(client, monkeypatch):
    async def fake_refresh(key="live"):
        raise RuntimeError("certlogin down")

    from services import betfair_auth
    monkeypatch.setattr(betfair_auth, "refresh_session", fake_refresh)

    r = client.post("/admin/control/relogin_rest")
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["executed"] is False
    assert "certlogin down" in body["note"]
