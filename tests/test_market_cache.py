"""Phase 2 — verify Betfair ESA delta reconstruction.

These tests use synthetic mcm messages — no Betfair credentials, no
network. The point is to lock the delta semantics so a future refactor
can't silently break.
"""
import asyncio

import pytest

from services.market_cache import MarketCache, MarketState, RunnerState


@pytest.mark.asyncio
async def test_apply_img_replaces_state():
    cache = MarketCache()
    msg = {
        "mc": [
            {
                "id": "1.111",
                "img": True,
                "marketDefinition": {
                    "eventTypeId": "7",
                    "venue": "Newmarket",
                    "marketType": "WIN",
                    "marketTime": "2026-05-29T14:15:00.000Z",
                    "runners": [{"id": 1001, "status": "ACTIVE"}],
                },
                "rc": [{"id": 1001, "ltp": 5.0}],
            }
        ]
    }
    touched = await cache.apply_mcm(msg)
    assert len(touched) == 1
    ms = touched[0]
    assert ms.market_id == "1.111"
    assert ms.event_type_id == "7"
    assert 1001 in ms.runners
    assert ms.runners[1001].ltp == 5.0


@pytest.mark.asyncio
async def test_apply_delta_updates_scalars():
    cache = MarketCache()
    await cache.apply_mcm({
        "mc": [{"id": "1.222", "img": True,
                "marketDefinition": {"eventTypeId": "1", "runners": [{"id": 1, "status": "ACTIVE"}]},
                "rc": [{"id": 1, "ltp": 2.0}]}]
    })
    await cache.apply_mcm({
        "mc": [{"id": "1.222", "rc": [{"id": 1, "ltp": 2.5, "tv": 100.0}]}]
    })
    ms = await cache.get("1.222")
    assert ms.runners[1].ltp == 2.5
    assert ms.runners[1].tv == 100.0


@pytest.mark.asyncio
async def test_atb_atl_price_point_ladder_with_removal():
    """size=0 must remove the price level."""
    cache = MarketCache()
    await cache.apply_mcm({
        "mc": [{"id": "1.333", "img": True,
                "marketDefinition": {"eventTypeId": "2"},
                "rc": [{"id": 5, "atb": [[3.0, 10.0], [3.1, 5.0]],
                        "atl": [[3.5, 20.0]]}]}]
    })
    ms = await cache.get("1.333")
    r = ms.runners[5]
    assert r.atb == {3.0: 10.0, 3.1: 5.0}
    assert r.atl == {3.5: 20.0}

    # Remove the 3.0 entry, add 3.2.
    await cache.apply_mcm({
        "mc": [{"id": "1.333",
                "rc": [{"id": 5, "atb": [[3.0, 0], [3.2, 7.0]]}]}]
    })
    ms = await cache.get("1.333")
    assert 3.0 not in ms.runners[5].atb
    assert ms.runners[5].atb[3.2] == 7.0
    assert ms.runners[5].atb[3.1] == 5.0


@pytest.mark.asyncio
async def test_batb_level_based_ladder_with_removal():
    cache = MarketCache()
    await cache.apply_mcm({
        "mc": [{"id": "1.444", "img": True,
                "marketDefinition": {"eventTypeId": "7"},
                "rc": [{"id": 7, "batb": [[0, 5.0, 100.0], [1, 5.1, 50.0]]}]}]
    })
    ms = await cache.get("1.444")
    r = ms.runners[7]
    assert r.batb == {0: [5.0, 100.0], 1: [5.1, 50.0]}

    # Update level 0, remove level 1.
    await cache.apply_mcm({
        "mc": [{"id": "1.444",
                "rc": [{"id": 7, "batb": [[0, 5.0, 75.0], [1, 5.1, 0]]}]}]
    })
    ms = await cache.get("1.444")
    assert ms.runners[7].batb == {0: [5.0, 75.0]}


@pytest.mark.asyncio
async def test_sp_sub_object():
    cache = MarketCache()
    await cache.apply_mcm({
        "mc": [{"id": "1.555", "img": True,
                "marketDefinition": {"eventTypeId": "7"},
                "rc": [{"id": 9, "sp": {"spn": 5.5, "spf": 5.7,
                                         "spb": [[5.5, 100.0]],
                                         "spl": [[5.7, 80.0]]}}]}]
    })
    ms = await cache.get("1.555")
    r = ms.runners[9]
    assert r.spn == 5.5
    assert r.spf == 5.7
    assert r.spb == {5.5: 100.0}
    assert r.spl == {5.7: 80.0}


@pytest.mark.asyncio
async def test_count_by_event_type_groups_correctly():
    cache = MarketCache()
    await cache.apply_mcm({
        "mc": [
            {"id": "h1", "img": True, "marketDefinition": {"eventTypeId": "7"}},
            {"id": "h2", "img": True, "marketDefinition": {"eventTypeId": "7"}},
            {"id": "f1", "img": True, "marketDefinition": {"eventTypeId": "1"}},
            {"id": "t1", "img": True, "marketDefinition": {"eventTypeId": "2"}},
        ]
    })
    counts = await cache.count_by_event_type()
    assert counts == {"7": 2, "1": 1, "2": 1}
    assert await cache.count() == 4


@pytest.mark.asyncio
async def test_all_summaries_filters_by_event_type():
    cache = MarketCache()
    await cache.apply_mcm({
        "mc": [
            {"id": "x", "img": True, "marketDefinition": {"eventTypeId": "7"}},
            {"id": "y", "img": True, "marketDefinition": {"eventTypeId": "1"}},
        ]
    })
    horse = await cache.all_summaries(event_type_id="7")
    assert len(horse) == 1 and horse[0]["market_id"] == "x"


@pytest.mark.asyncio
async def test_remove_closed_evicts():
    cache = MarketCache()
    await cache.apply_mcm({
        "mc": [
            {"id": "open", "img": True,
             "marketDefinition": {"eventTypeId": "7", "status": "OPEN"}},
            {"id": "closed", "img": True,
             "marketDefinition": {"eventTypeId": "7", "status": "CLOSED"}},
        ]
    })
    removed = await cache.remove_closed()
    assert removed == 1
    assert await cache.count() == 1


def test_runner_best_available_from_level_ladder():
    r = RunnerState(selection_id=1)
    r.batb = {0: [5.0, 100.0], 1: [4.9, 50.0]}
    r.batl = {0: [5.1, 80.0], 1: [5.2, 40.0]}
    assert r.best_available_to_back(2) == [[5.0, 100.0], [4.9, 50.0]]
    assert r.best_available_to_lay(2) == [[5.1, 80.0], [5.2, 40.0]]


def test_runner_best_available_fallback_to_price_point():
    r = RunnerState(selection_id=2)
    # No batb — must derive from atb (highest price first).
    r.atb = {3.0: 10.0, 3.2: 5.0, 2.8: 20.0}
    assert r.best_available_to_back(2) == [[3.2, 5.0], [3.0, 10.0]]
    # No batl — atl ascending (lowest first).
    r.atl = {3.5: 100.0, 3.6: 50.0, 3.4: 70.0}
    assert r.best_available_to_lay(2) == [[3.4, 70.0], [3.5, 100.0]]
