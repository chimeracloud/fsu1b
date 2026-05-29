"""Phase 2 — SSE per-sport endpoints + /markets reads.

SSE-open tests are not run via TestClient (sync TestClient hangs on
the generator's wait). Route registration is verified directly and SSE
behaviour is exercised at unit level (via app_state.broadcast).

The /markets and /stream/snapshot endpoints are exercised against a
primed cache.
"""
import asyncio

import pytest

from main import app
from services.market_cache import market_cache


def test_sse_routes_are_registered():
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/stream/horse-racing" in paths
    assert "/stream/football" in paths
    assert "/stream/tennis" in paths
    assert "/stream/all" in paths


def test_markets_empty_at_boot(client):
    r = client.get("/markets")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["markets"] == []


def test_markets_rejects_unknown_sport(client):
    r = client.get("/markets?sport=cricket")
    assert r.status_code == 400


def test_market_by_id_returns_404_when_missing(client):
    r = client.get("/markets/1.does-not-exist")
    assert r.status_code == 404


def test_snapshot_with_primed_cache(client):
    async def prime():
        await market_cache.apply_mcm({
            "mc": [{"id": "1.primed", "img": True,
                     "marketDefinition": {"eventTypeId": "7", "venue": "Ascot",
                                          "marketType": "WIN"},
                     "rc": [{"id": 42, "ltp": 4.0}]}]
        })

    asyncio.run(prime())
    try:
        r = client.get("/stream/snapshot?sport=horse-racing")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["markets"][0]["market_id"] == "1.primed"
        assert body["markets"][0]["venue"] == "Ascot"
    finally:
        asyncio.run(market_cache.reset_for_test())


def test_market_by_id_returns_full_after_priming(client):
    async def prime():
        await market_cache.apply_mcm({
            "mc": [{"id": "1.full", "img": True,
                     "marketDefinition": {"eventTypeId": "1", "venue": "Wembley",
                                          "marketType": "MATCH_ODDS"},
                     "rc": [{"id": 100, "ltp": 1.8, "atb": [[1.8, 100.0]]}]}]
        })

    asyncio.run(prime())
    try:
        r = client.get("/markets/1.full")
        assert r.status_code == 200
        body = r.json()
        assert body["sport"] == "football"
        assert body["event_type_id"] == "1"
        assert body["runners"][0]["selection_id"] == 100
        assert body["runners"][0]["ltp"] == 1.8
    finally:
        asyncio.run(market_cache.reset_for_test())


@pytest.mark.asyncio
async def test_sse_broadcast_routes_to_per_sport_channel():
    """When stream_client broadcasts to a sport channel, only that channel
    plus 'all' receives the event."""
    from core.state import app_state

    horse_q = await app_state.subscribe("horse-racing")
    tennis_q = await app_state.subscribe("tennis")
    all_q = await app_state.subscribe("all")

    await app_state.broadcast("horse-racing", {"event": "market_change", "x": 1})

    assert horse_q.qsize() == 1
    assert all_q.qsize() == 1
    assert tennis_q.qsize() == 0

    msg = await horse_q.get()
    assert msg["x"] == 1

    await app_state.unsubscribe("horse-racing", horse_q)
    await app_state.unsubscribe("tennis", tennis_q)
    await app_state.unsubscribe("all", all_q)
