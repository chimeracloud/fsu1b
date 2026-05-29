"""
Phase 3 — DELAYED-key read endpoints.

These tests patch the `services.betfair_rest` high-level functions so
no real Betfair call is made. They assert:

  * The route is registered.
  * Query params are translated into bfl kwargs correctly.
  * The envelope shape is returned to the caller unchanged.
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


def _fake_envelope(extra: dict | None = None) -> dict:
    env = {
        "ok": True,
        "dry_run": False,
        "gateway_ts": "2026-05-29T13:00:00Z",
        "latency_ms": 12.3,
        "betfair": {"fixture": True, **(extra or {})},
    }
    return env


def test_current_orders_passes_filters(client, monkeypatch):
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return _fake_envelope({"echo": kwargs})

    from services import betfair_rest
    monkeypatch.setattr(betfair_rest, "list_current_orders", fake)

    r = client.get(
        "/orders/current?market_id=1.111&customer_strategy_refs=strat1,strat2&from_record=10&record_count=50"
    )
    assert r.status_code == 200
    assert captured["market_ids"] == ["1.111"]
    assert captured["customer_strategy_refs"] == ["strat1", "strat2"]
    assert captured["from_record"] == 10
    assert captured["record_count"] == 50


def test_cleared_orders_with_settled_range(client, monkeypatch):
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return _fake_envelope()

    from services import betfair_rest
    monkeypatch.setattr(betfair_rest, "list_cleared_orders", fake)

    r = client.get(
        "/orders/cleared?bet_status=SETTLED&market_id=1.222"
        "&settled_from=2026-05-01T00:00:00Z&settled_to=2026-05-29T00:00:00Z"
    )
    assert r.status_code == 200
    assert captured["bet_status"] == "SETTLED"
    assert captured["market_ids"] == ["1.222"]
    assert captured["settled_date_range"] == {
        "from": "2026-05-01T00:00:00Z",
        "to": "2026-05-29T00:00:00Z",
    }


def test_account_funds_uk_default(client, monkeypatch):
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return _fake_envelope({"availableToBetBalance": 250.0})

    from services import betfair_rest
    monkeypatch.setattr(betfair_rest, "get_account_funds", fake)

    r = client.get("/account/funds")
    assert r.status_code == 200
    assert captured["wallet"] == "UK"
    assert r.json()["betfair"]["availableToBetBalance"] == 250.0


def test_account_statement_with_range(client, monkeypatch):
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return _fake_envelope()

    from services import betfair_rest
    monkeypatch.setattr(betfair_rest, "get_account_statement", fake)

    r = client.get(
        "/account/statement?item_date_from=2026-05-01T00:00:00Z"
        "&item_date_to=2026-05-29T00:00:00Z&include_item=EXCHANGE"
    )
    assert r.status_code == 200
    assert captured["include_item"] == "EXCHANGE"
    assert captured["item_date_range"] == {
        "from": "2026-05-01T00:00:00Z",
        "to": "2026-05-29T00:00:00Z",
    }


def test_catalogue_passes_filter_and_projection(client, monkeypatch):
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return _fake_envelope({"markets": []})

    from services import betfair_rest
    monkeypatch.setattr(betfair_rest, "list_market_catalogue", fake)

    r = client.get(
        "/catalogue/markets?event_type_id=7&country=GB&market_type=WIN&max_results=50"
    )
    assert r.status_code == 200
    assert captured["filter"]["eventTypeIds"] == ["7"]
    assert captured["filter"]["marketCountries"] == ["GB"]
    assert captured["filter"]["marketTypeCodes"] == ["WIN"]
    assert captured["max_results"] == 50
    assert captured["sort"] == "FIRST_TO_START"
    assert "RUNNER_DESCRIPTION" in captured["market_projection"]


def test_catalogue_in_play_only(client, monkeypatch):
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return _fake_envelope()

    from services import betfair_rest
    monkeypatch.setattr(betfair_rest, "list_market_catalogue", fake)

    r = client.get("/catalogue/markets?in_play_only=true")
    assert r.status_code == 200
    assert captured["filter"]["inPlayOnly"] is True
