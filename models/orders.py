"""
Pydantic v2 models for Phase 3 — orders, account, catalogue.

The gateway is a tape recorder. Inputs are validated for shape, then
passed through to Betfair via betfairlightweight. Responses are the
Betfair payload, wrapped with `{ok, latency_ms, gateway_ts, dry_run,
betfair}`. No interpretation, no calculation.

Idempotency:
  `customer_ref` is the market-level idempotency key. Betfair caches
  it for ~5 minutes and returns the same result on retry. Live Betting
  Control owns this — FSU1B passes it through.

DRY_RUN:
  Global `Settings.dry_run` is consulted on every order endpoint. When
  True, no Betfair call is made; the gateway returns a simulated
  success that mirrors the real response shape. Operators flip the
  flag via `PUT /admin/config`.
"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Side = Literal["BACK", "LAY"]
OrderType = Literal["LIMIT", "LIMIT_ON_CLOSE", "MARKET_ON_CLOSE"]
PersistenceType = Literal["LAPSE", "PERSIST", "MARKET_ON_CLOSE"]
TimeInForce = Literal["FILL_OR_KILL"]


# ── place ────────────────────────────────────────────────────────────────


class LimitOrder(BaseModel):
    size: float = Field(..., gt=0)
    price: float = Field(..., gt=1.0, lt=1001.0)
    persistence_type: PersistenceType = "LAPSE"
    time_in_force: TimeInForce | None = None
    min_fill_size: float | None = None
    bet_target_type: Literal["BACKERS_PROFIT", "PAYOUT"] | None = None
    bet_target_size: float | None = None


class LimitOnCloseOrder(BaseModel):
    liability: float = Field(..., gt=0)
    price: float = Field(..., gt=1.0, lt=1001.0)


class MarketOnCloseOrder(BaseModel):
    liability: float = Field(..., gt=0)


class PlaceInstruction(BaseModel):
    selection_id: int
    side: Side
    order_type: OrderType = "LIMIT"
    handicap: float = 0.0
    limit_order: LimitOrder | None = None
    limit_on_close_order: LimitOnCloseOrder | None = None
    market_on_close_order: MarketOnCloseOrder | None = None
    customer_order_ref: str | None = Field(
        default=None,
        max_length=32,
        description="Echoed back on settlement; identifies the strategy's order.",
    )


class PlaceOrdersRequest(BaseModel):
    market_id: str
    instructions: list[PlaceInstruction] = Field(..., min_length=1, max_length=200)
    customer_ref: str | None = Field(
        default=None,
        max_length=32,
        description="Market-level idempotency key (alphanumeric + . - _ + *).",
    )
    customer_strategy_ref: str | None = Field(
        default=None,
        max_length=15,
        description="Echoed on every order for analytics — strategy identifier.",
    )
    market_version: int | None = Field(
        default=None,
        description="Reject if Betfair has rolled the market past this version.",
    )
    async_: bool = Field(
        default=False,
        alias="async",
        description="Server-side async placement (Betfair async option).",
    )


# ── cancel / replace ─────────────────────────────────────────────────────


class CancelInstruction(BaseModel):
    bet_id: str
    size_reduction: float | None = Field(
        default=None, gt=0, description="Partial cancel; null = cancel all unmatched."
    )


class CancelOrdersRequest(BaseModel):
    market_id: str | None = None
    instructions: list[CancelInstruction] = Field(default_factory=list)
    customer_ref: str | None = Field(default=None, max_length=32)


class ReplaceInstruction(BaseModel):
    bet_id: str
    new_price: float = Field(..., gt=1.0, lt=1001.0)


class ReplaceOrdersRequest(BaseModel):
    market_id: str
    instructions: list[ReplaceInstruction] = Field(..., min_length=1, max_length=60)
    customer_ref: str | None = Field(default=None, max_length=32)
    market_version: int | None = None
    async_: bool = Field(default=False, alias="async")


# ── envelope returned by every order endpoint ───────────────────────────


class OrderResponse(BaseModel):
    ok: bool
    dry_run: bool
    gateway_ts: datetime
    latency_ms: float
    betfair: dict[str, Any] | list[Any] | None
