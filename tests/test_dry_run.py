"""
Phase 3 — DRY_RUN behaviour for order endpoints.

When `Settings.dry_run=True`, /orders/place|cancel|replace must NOT
call Betfair. Return a simulated success envelope that mirrors the
real response shape so downstream consumers can be tested end-to-end
without real money.
"""
import pytest

from core.config import replace_settings, reset_settings_for_test
from core.state import reset_state_for_test


@pytest.fixture(autouse=True)
def _ensure_dry_run():
    reset_settings_for_test()
    reset_state_for_test()
    replace_settings(dry_run=True)
    yield
    reset_settings_for_test()
    reset_state_for_test()


def test_dry_run_place_returns_simulated_success(client):
    body = {
        "market_id": "1.111",
        "instructions": [
            {
                "selection_id": 12345,
                "side": "LAY",
                "order_type": "LIMIT",
                "limit_order": {"size": 2.0, "price": 5.0, "persistence_type": "LAPSE"},
                "customer_order_ref": "test-1",
            }
        ],
        "customer_ref": "abc-123",
        "customer_strategy_ref": "strat-1",
    }
    r = client.post("/orders/place", json=body)
    assert r.status_code == 200
    env = r.json()
    assert env["dry_run"] is True
    assert env["ok"] is True
    assert env["latency_ms"] == 0.0
    bf = env["betfair"]
    assert bf["status"] == "SUCCESS"
    assert bf["marketId"] == "1.111"
    assert bf["customerRef"] == "abc-123"
    assert bf["customerStrategyRef"] == "strat-1"
    assert len(bf["instructionReports"]) == 1
    assert bf["instructionReports"][0]["status"] == "SUCCESS"
    assert bf["instructionReports"][0]["betId"].startswith("DRY-")


def test_dry_run_cancel_simulates(client):
    body = {
        "market_id": "1.222",
        "instructions": [{"bet_id": "BET-1"}, {"bet_id": "BET-2", "size_reduction": 1.0}],
        "customer_ref": "c-1",
    }
    r = client.post("/orders/cancel", json=body)
    assert r.status_code == 200
    env = r.json()
    assert env["dry_run"] is True
    assert env["betfair"]["status"] == "SUCCESS"
    assert len(env["betfair"]["instructionReports"]) == 2


def test_dry_run_replace_simulates(client):
    body = {
        "market_id": "1.333",
        "instructions": [{"bet_id": "BET-9", "new_price": 4.2}],
        "customer_ref": "r-1",
    }
    r = client.post("/orders/replace", json=body)
    assert r.status_code == 200
    env = r.json()
    assert env["dry_run"] is True
    assert env["betfair"]["status"] == "SUCCESS"
    assert len(env["betfair"]["instructionReports"]) == 1


def test_place_validates_limit_requires_limit_order(client):
    """LIMIT order_type without limit_order sub-object must 422."""
    body = {
        "market_id": "1.444",
        "instructions": [
            {"selection_id": 99, "side": "BACK", "order_type": "LIMIT"},
        ],
    }
    r = client.post("/orders/place", json=body)
    assert r.status_code == 422


def test_place_validates_size_must_be_positive(client):
    body = {
        "market_id": "1.555",
        "instructions": [
            {
                "selection_id": 99,
                "side": "BACK",
                "order_type": "LIMIT",
                "limit_order": {"size": 0, "price": 2.0},
            },
        ],
    }
    r = client.post("/orders/place", json=body)
    assert r.status_code == 422


def test_place_validates_price_range(client):
    body = {
        "market_id": "1.666",
        "instructions": [
            {
                "selection_id": 99,
                "side": "BACK",
                "order_type": "LIMIT",
                "limit_order": {"size": 2.0, "price": 1.0},  # must be >1.0
            },
        ],
    }
    r = client.post("/orders/place", json=body)
    assert r.status_code == 422
