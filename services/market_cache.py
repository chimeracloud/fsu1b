"""
In-memory market cache with full Betfair ESA delta reconstruction.

Ported from FSU1A. Adapted: every MarketState carries the eventTypeId
(extracted from market_definition) so the stream router can route to
per-sport SSE channels without re-parsing.

Delta encoding rules (per Betfair Exchange Streaming API spec):

Level-based ladders — batb, batl, bdatb, bdatl
  Each update is a triple: [level, price, size]
  Keyed by level (int). Level 0 = best price.
  size == 0 → remove that level entry.

Price-point ladders — atb, atl, trd, spb, spl
  Each update is a pair: [price, size]
  Keyed by price (float). size == 0 → remove that price entry.

Scalar fields — ltp, tv, spn, spf
  Sent only when changed; absence means unchanged.

img == True on a MarketChange → replace entire market state from scratch.

sp sub-object within a RunnerChange:
  sp.spn, sp.spf — scalar SP near/far
  sp.spb, sp.spl — price-point SP back/lay bets placed
  sp.bsp         — calculated BSP

Segmentation (segmentType: SEG_START | SEG | SEG_END | null):
  Apply all mc[] deltas immediately regardless of segment.
  Only update stored clk on non-segmented (null) or SEG_END messages
  (handled in stream_client, not here).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)
UTC = timezone.utc

LevelLadder = dict  # int   → [price: float, size: float]
PriceLadder = dict  # float → size: float


@dataclass
class RunnerState:
    """In-memory state for a single runner (selection)."""

    selection_id: int
    handicap: float = 0.0
    status: str = "ACTIVE"

    # Level-based ladders.
    batb: LevelLadder = field(default_factory=dict)
    batl: LevelLadder = field(default_factory=dict)
    bdatb: LevelLadder = field(default_factory=dict)
    bdatl: LevelLadder = field(default_factory=dict)

    # Price-point ladders.
    atb: PriceLadder = field(default_factory=dict)
    atl: PriceLadder = field(default_factory=dict)
    trd: PriceLadder = field(default_factory=dict)
    spb: PriceLadder = field(default_factory=dict)
    spl: PriceLadder = field(default_factory=dict)

    # Scalars.
    ltp: Optional[float] = None
    tv: Optional[float] = None
    spn: Optional[float] = None
    spf: Optional[float] = None
    bsp: Optional[float] = None

    def apply_runner_change(self, rc: dict) -> None:
        if "status" in rc:
            self.status = rc["status"]
        if "ltp" in rc:
            self.ltp = rc["ltp"]
        if "tv" in rc:
            self.tv = rc["tv"]
        if "spn" in rc:
            self.spn = rc["spn"]
        if "spf" in rc:
            self.spf = rc["spf"]

        # Level-based: [level, price, size]
        for fname in ("batb", "batl", "bdatb", "bdatl"):
            if fname not in rc:
                continue
            ladder = getattr(self, fname)
            for triple in rc[fname]:
                level, price, size = int(triple[0]), float(triple[1]), float(triple[2])
                if size == 0:
                    ladder.pop(level, None)
                else:
                    ladder[level] = [price, size]

        # Price-point: [price, size]
        for fname in ("atb", "atl", "trd"):
            if fname not in rc:
                continue
            ladder = getattr(self, fname)
            for pair in rc[fname]:
                price, size = float(pair[0]), float(pair[1])
                if size == 0:
                    ladder.pop(price, None)
                else:
                    ladder[price] = size

        # SP sub-object.
        if "sp" in rc:
            sp = rc["sp"]
            if "spn" in sp:
                self.spn = sp["spn"]
            if "spf" in sp:
                self.spf = sp["spf"]
            if "bsp" in sp:
                self.bsp = sp["bsp"]
            for fname in ("spb", "spl"):
                if fname not in sp:
                    continue
                ladder = getattr(self, fname)
                for pair in sp[fname]:
                    price, size = float(pair[0]), float(pair[1])
                    if size == 0:
                        ladder.pop(price, None)
                    else:
                        ladder[price] = size

    # ── Derived views ────────────────────────────────────────────────────

    def best_available_to_back(self, depth: int = 3) -> list:
        if self.batb:
            return [self.batb[lvl] for lvl in sorted(self.batb)[:depth]]
        if self.atb:
            prices = sorted(self.atb.keys(), reverse=True)[:depth]
            return [[p, self.atb[p]] for p in prices]
        return []

    def best_available_to_lay(self, depth: int = 3) -> list:
        if self.batl:
            return [self.batl[lvl] for lvl in sorted(self.batl)[:depth]]
        if self.atl:
            prices = sorted(self.atl.keys())[:depth]
            return [[p, self.atl[p]] for p in prices]
        return []

    def traded_ladder(self) -> list:
        return [[p, self.trd[p]] for p in sorted(self.trd.keys())]

    def to_dict(self) -> dict:
        return {
            "selection_id": self.selection_id,
            "handicap": self.handicap,
            "status": self.status,
            "ltp": self.ltp,
            "tv": self.tv,
            "spn": self.spn,
            "spf": self.spf,
            "bsp": self.bsp,
            "back": self.best_available_to_back(3),
            "lay": self.best_available_to_lay(3),
            "atb": sorted(
                [[p, s] for p, s in self.atb.items()], key=lambda x: -x[0]
            ),
            "atl": sorted(
                [[p, s] for p, s in self.atl.items()], key=lambda x: x[0]
            ),
            "trd": self.traded_ladder(),
        }


@dataclass
class MarketState:
    """In-memory state for a single market."""

    market_id: str
    market_definition: dict = field(default_factory=dict)
    runners: dict = field(default_factory=dict)
    status: Optional[str] = None
    total_volume: Optional[float] = None
    last_update_at: Optional[datetime] = None

    def _get_or_create_runner(self, sid: int, hc: float = 0.0) -> RunnerState:
        if sid not in self.runners:
            self.runners[sid] = RunnerState(selection_id=sid, handicap=hc)
        return self.runners[sid]

    def apply_market_change(self, mc: dict) -> None:
        if mc.get("img"):
            self.runners.clear()
            self.market_definition = {}
            self.status = None
            self.total_volume = None

        if "marketDefinition" in mc:
            md = mc["marketDefinition"]
            self.market_definition.update(md)
            for rd in md.get("runners", []):
                sid = int(rd["id"])
                runner = self._get_or_create_runner(sid, float(rd.get("hc", 0.0)))
                if "status" in rd:
                    runner.status = rd["status"]

        if "status" in mc:
            self.status = mc["status"]

        if "tv" in mc:
            self.total_volume = mc["tv"]

        for rc in mc.get("rc", []):
            sid = int(rc["id"])
            hc = float(rc.get("hc", 0.0))
            runner = self._get_or_create_runner(sid, hc)
            runner.apply_runner_change(rc)

        self.last_update_at = datetime.now(UTC)

    # ── Derived views ────────────────────────────────────────────────────

    @property
    def event_type_id(self) -> Optional[str]:
        v = self.market_definition.get("eventTypeId")
        return str(v) if v is not None else None

    @property
    def effective_status(self) -> str:
        return self.status or self.market_definition.get("status", "UNKNOWN")

    def _display_name(self) -> str:
        md = self.market_definition
        venue = md.get("venue")
        market_time = md.get("marketTime")
        market_type = md.get("marketType")
        if market_time and venue:
            try:
                t = datetime.fromisoformat(str(market_time).replace("Z", "+00:00"))
                return f"{t.strftime('%H:%M')} {venue}"
            except Exception:  # noqa: BLE001
                pass
        return f"{venue or ''} {market_type or ''}".strip() or self.market_id

    def to_summary(self) -> dict:
        md = self.market_definition
        return {
            "market_id": self.market_id,
            "event_type_id": self.event_type_id,
            "event_id": md.get("eventId"),
            "country_code": md.get("countryCode"),
            "venue": md.get("venue"),
            "market_type": md.get("marketType"),
            "name": self._display_name(),
            "status": self.effective_status,
            "in_play": bool(md.get("inPlay", False)),
            "market_time": md.get("marketTime"),
            "bsp_market": bool(md.get("bspMarket", False)),
            "total_matched": self.total_volume,
            "runner_count": len(self.runners),
            "last_update_at": (
                self.last_update_at.isoformat() if self.last_update_at else None
            ),
        }

    def to_full(self) -> dict:
        d = self.to_summary()
        d["market_definition"] = dict(self.market_definition)
        d["runners"] = [r.to_dict() for r in self.runners.values()]
        return d


class MarketCache:
    """Thread-safe in-memory store for active market states.

    All mutations serialised through an asyncio.Lock.
    """

    def __init__(self) -> None:
        self._markets: dict[str, MarketState] = {}
        self._lock = asyncio.Lock()

    async def apply_mcm(self, msg: dict) -> list[MarketState]:
        """Apply a MarketChangeMessage. Returns the set of MarketStates touched."""
        touched: list[MarketState] = []
        async with self._lock:
            for mc in msg.get("mc", []):
                mid: str = mc["id"]
                if mid not in self._markets:
                    self._markets[mid] = MarketState(market_id=mid)
                ms = self._markets[mid]
                ms.apply_market_change(mc)
                touched.append(ms)
        return touched

    async def get(self, market_id: str) -> Optional[MarketState]:
        async with self._lock:
            return self._markets.get(market_id)

    async def all_summaries(self, event_type_id: Optional[str] = None) -> list[dict]:
        async with self._lock:
            items = self._markets.values()
            if event_type_id is not None:
                items = [m for m in items if m.event_type_id == event_type_id]
            return [m.to_summary() for m in items]

    async def count(self) -> int:
        async with self._lock:
            return len(self._markets)

    async def count_by_event_type(self) -> dict[str, int]:
        async with self._lock:
            out: dict[str, int] = {}
            for m in self._markets.values():
                et = m.event_type_id or "unknown"
                out[et] = out.get(et, 0) + 1
            return out

    async def remove_closed(self) -> int:
        async with self._lock:
            to_remove = [
                mid for mid, ms in self._markets.items()
                if ms.market_definition.get("status") == "CLOSED"
            ]
            for mid in to_remove:
                del self._markets[mid]
            return len(to_remove)

    async def reset_for_test(self) -> None:
        async with self._lock:
            self._markets.clear()


# Module-level singleton.
market_cache = MarketCache()
