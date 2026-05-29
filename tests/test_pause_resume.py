"""
Phase 3 — pause/resume kill switch for order writes.

`POST /admin/control/pause` sets a flag that makes
/orders/place|cancel|replace return 503 without touching Betfair.
Read endpoints and the stream are unaffected.
"""
import pytest

from core.config import replace_settings, reset_settings_for_test
from core.state import app_state, reset_state_for_test


@pytest.fixture(autouse=True)
def _isolated():
    reset_settings_for_test()
    reset_state_for_test()
    replace_settings(dry_run=True)
    yield
    reset_settings_for_test()
    reset_state_for_test()


def test_pause_then_place_returns_503(client):
    pr = client.post("/admin/control/pause")
    assert pr.status_code == 200
    assert pr.json()["executed"] is True

    body = {
        "market_id": "1.x",
        "instructions": [
            {
                "selection_id": 1,
                "side": "LAY",
                "order_type": "LIMIT",
                "limit_order": {"size": 2.0, "price": 5.0},
            }
        ],
    }
    r = client.post("/orders/place", json=body)
    assert r.status_code == 503
    assert "paused" in r.json()["detail"]


def test_pause_does_not_block_cancels_or_replaces_until_explicitly_paused(client):
    """Sanity: with no pause, cancels work (in DRY_RUN)."""
    body = {"market_id": "1.y", "instructions": [{"bet_id": "B1"}]}
    r = client.post("/orders/cancel", json=body)
    assert r.status_code == 200


def test_pause_blocks_cancel_and_replace(client):
    client.post("/admin/control/pause")
    cr = client.post("/orders/cancel", json={"market_id": "1.z", "instructions": [{"bet_id": "B1"}]})
    rr = client.post("/orders/replace", json={
        "market_id": "1.z",
        "instructions": [{"bet_id": "B1", "new_price": 4.0}],
    })
    assert cr.status_code == 503
    assert rr.status_code == 503


def test_resume_restores_acceptance(client):
    client.post("/admin/control/pause")
    rr = client.post("/admin/control/resume")
    assert rr.status_code == 200
    assert rr.json()["executed"] is True

    body = {
        "market_id": "1.r",
        "instructions": [{
            "selection_id": 1, "side": "BACK", "order_type": "LIMIT",
            "limit_order": {"size": 2.0, "price": 3.0},
        }],
    }
    r = client.post("/orders/place", json=body)
    assert r.status_code == 200


def test_double_pause_idempotent(client):
    r1 = client.post("/admin/control/pause")
    r2 = client.post("/admin/control/pause")
    assert r1.json()["executed"] is True
    assert r2.json()["executed"] is False  # already paused
    assert app_state.orders_paused is True


def test_resume_when_not_paused_reports_not_executed(client):
    r = client.post("/admin/control/resume")
    assert r.json()["executed"] is False
