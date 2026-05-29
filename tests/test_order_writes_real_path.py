"""
Phase 3 — non-DRY_RUN order endpoints.

When `dry_run=False`, /orders/place|cancel|replace must call the
real betfair_rest helper. Tests patch that helper to confirm:

  * The Pydantic body is correctly marshalled into the payload dict.
  * The helper is awaited.
  * The envelope returned by the helper flows back to the caller.

No Betfair call is made — `services.betfair_rest.place_orders` etc.
are patched at the module level.
"""
import pytest

from core.config import replace_settings, reset_settings_for_test
from core.state import reset_state_for_test


@pytest.fixture(autouse=True)
def _isolated():
    reset_settings_for_test()
    reset_state_for_test()
    replace_settings(dry_run=False)  # real path
    yield
    reset_settings_for_test()
    reset_state_for_test()


def _envelope():
    return {
        "ok": True,
        "dry_run": False,
        "gateway_ts": "2026-05-29T13:00:00Z",
        "latency_ms": 9.9,
        "betfair": {"status": "SUCCESS", "marketId": "captured-by-fake"},
    }


def test_place_payload_marshalling(client, monkeypatch):
    captured = {}

    async def fake_place(payload):
        captured["payload"] = payload
        return _envelope()

    from services import betfair_rest
    monkeypatch.setattr(betfair_rest, "place_orders", fake_place)

    body = {
        "market_id": "1.111",
        "instructions": [
            {
                "selection_id": 42,
                "side": "BACK",
                "order_type": "LIMIT",
                "limit_order": {"size": 5.0, "price": 3.5, "persistence_type": "PERSIST"},
                "customer_order_ref": "x-1",
            }
        ],
        "customer_ref": "ref-1",
        "customer_strategy_ref": "strat-A",
        "async": True,
    }
    r = client.post("/orders/place", json=body)
    assert r.status_code == 200

    payload = captured["payload"]
    assert payload["market_id"] == "1.111"
    assert payload["customer_ref"] == "ref-1"
    assert payload["customer_strategy_ref"] == "strat-A"
    # by_alias=True → 'async' is preserved (not 'async_')
    assert payload["async"] is True
    ins = payload["instructions"][0]
    assert ins["selection_id"] == 42
    assert ins["side"] == "BACK"
    assert ins["limit_order"]["size"] == 5.0
    assert ins["limit_order"]["price"] == 3.5
    assert ins["customer_order_ref"] == "x-1"


def test_cancel_payload_marshalling(client, monkeypatch):
    captured = {}

    async def fake_cancel(payload):
        captured["payload"] = payload
        return _envelope()

    from services import betfair_rest
    monkeypatch.setattr(betfair_rest, "cancel_orders", fake_cancel)

    body = {
        "market_id": "1.222",
        "instructions": [
            {"bet_id": "B1"},
            {"bet_id": "B2", "size_reduction": 1.5},
        ],
        "customer_ref": "c-1",
    }
    r = client.post("/orders/cancel", json=body)
    assert r.status_code == 200
    p = captured["payload"]
    assert p["market_id"] == "1.222"
    assert p["instructions"][0]["bet_id"] == "B1"
    assert p["instructions"][1]["size_reduction"] == 1.5


def test_replace_payload_marshalling(client, monkeypatch):
    captured = {}

    async def fake_replace(payload):
        captured["payload"] = payload
        return _envelope()

    from services import betfair_rest
    monkeypatch.setattr(betfair_rest, "replace_orders", fake_replace)

    body = {
        "market_id": "1.333",
        "instructions": [{"bet_id": "B9", "new_price": 4.2}],
        "customer_ref": "r-1",
        "market_version": 7,
    }
    r = client.post("/orders/replace", json=body)
    assert r.status_code == 200
    p = captured["payload"]
    assert p["market_version"] == 7
    assert p["instructions"][0]["new_price"] == 4.2


def test_place_upstream_error_becomes_502(client, monkeypatch):
    async def boom(payload):
        raise RuntimeError("betfair unreachable")

    from services import betfair_rest
    monkeypatch.setattr(betfair_rest, "place_orders", boom)

    body = {
        "market_id": "1.444",
        "instructions": [
            {
                "selection_id": 1,
                "side": "BACK",
                "order_type": "LIMIT",
                "limit_order": {"size": 2.0, "price": 3.0},
            }
        ],
    }
    r = client.post("/orders/place", json=body)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["ok"] is False
    assert detail["upstream"] == "betfair"
    assert detail["error"] == "RuntimeError"
    assert "unreachable" in detail["message"]
