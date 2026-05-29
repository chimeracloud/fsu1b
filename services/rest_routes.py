"""
Set 3 — CONTENT (continued): REST reads + order writes.

DELAYED-key reads:
  GET /orders/current
  GET /orders/cleared
  GET /account/funds
  GET /account/statement
  GET /catalogue/markets

LIVE-key writes (wagering activity):
  POST /orders/place
  POST /orders/cancel
  POST /orders/replace

All write endpoints respect `Settings.dry_run`. When True, no Betfair
call is made; the response is simulated. Operators flip the flag via
`PUT /admin/config` (in-memory in Phase 3; GCS in Phase 4).

The gateway returns Betfair's payload unchanged, wrapped in
`{ok, dry_run, gateway_ts, latency_ms, betfair}`. Calculation,
reconciliation and commission live elsewhere (Bible §24).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from core.state import app_state
from models.orders import (
    CancelOrdersRequest,
    OrderResponse,
    PlaceOrdersRequest,
    ReplaceOrdersRequest,
)
from services import betfair_rest


def _check_orders_open() -> None:
    """Reject writes when the operator has paused order acceptance."""
    if app_state.orders_paused:
        raise HTTPException(
            status_code=503,
            detail="orders paused — POST /admin/control/resume to re-enable",
        )

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rest"])


def _to_bfl_kwargs(model_dump: dict) -> dict:
    """Translate snake_case Pydantic fields to bfl's camelCase kwargs.

    betfairlightweight uses Pythonic snake_case in its own helpers, so
    no translation is needed for the top-level kwargs we pass through;
    however, nested dicts (limit_order, instructions) must keep their
    snake_case keys for bfl's own internal serialiser. So we pass
    `model_dump(mode='python')` as-is.
    """
    return {k: v for k, v in model_dump.items() if v is not None}


# ─────────────────────────────────────────────────────────────────────────────
# DELAYED-key reads
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/orders/current")
async def orders_current(
    market_id: Optional[str] = Query(default=None),
    customer_order_refs: Optional[str] = Query(
        default=None,
        description="Comma-separated list of customer_order_ref to filter on.",
    ),
    customer_strategy_refs: Optional[str] = Query(
        default=None,
        description="Comma-separated list of customer_strategy_ref to filter on.",
    ),
    from_record: int = Query(default=0, ge=0),
    record_count: int = Query(default=1000, ge=1, le=1000),
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "from_record": from_record,
        "record_count": record_count,
    }
    if market_id:
        kwargs["market_ids"] = [market_id]
    if customer_order_refs:
        kwargs["customer_order_refs"] = [s.strip() for s in customer_order_refs.split(",") if s.strip()]
    if customer_strategy_refs:
        kwargs["customer_strategy_refs"] = [s.strip() for s in customer_strategy_refs.split(",") if s.strip()]
    return await betfair_rest.list_current_orders(**kwargs)


@router.get("/orders/cleared")
async def orders_cleared(
    bet_status: str = Query(default="SETTLED", description="SETTLED | VOIDED | LAPSED | CANCELLED"),
    market_id: Optional[str] = Query(default=None),
    settled_from: Optional[str] = Query(default=None, description="ISO 8601 UTC"),
    settled_to: Optional[str] = Query(default=None, description="ISO 8601 UTC"),
    from_record: int = Query(default=0, ge=0),
    record_count: int = Query(default=1000, ge=1, le=1000),
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "bet_status": bet_status,
        "from_record": from_record,
        "record_count": record_count,
    }
    if market_id:
        kwargs["market_ids"] = [market_id]
    if settled_from or settled_to:
        kwargs["settled_date_range"] = {"from": settled_from, "to": settled_to}
    return await betfair_rest.list_cleared_orders(**kwargs)


@router.get("/account/funds")
async def account_funds(
    wallet: str = Query(default="UK", description="UK | AUSTRALIAN"),
) -> dict[str, Any]:
    return await betfair_rest.get_account_funds(wallet=wallet)


@router.get("/account/statement")
async def account_statement(
    locale: str = Query(default="en"),
    from_record: int = Query(default=0, ge=0),
    record_count: int = Query(default=100, ge=1, le=100),
    item_date_from: Optional[str] = Query(default=None),
    item_date_to: Optional[str] = Query(default=None),
    include_item: str = Query(
        default="ALL",
        description="ALL | DEPOSITS_WITHDRAWALS | EXCHANGE | POKER_ROOM",
    ),
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "locale": locale,
        "from_record": from_record,
        "record_count": record_count,
        "include_item": include_item,
    }
    if item_date_from or item_date_to:
        kwargs["item_date_range"] = {"from": item_date_from, "to": item_date_to}
    return await betfair_rest.get_account_statement(**kwargs)


@router.get("/catalogue/markets")
async def catalogue_markets(
    event_type_id: Optional[str] = Query(default=None),
    country: Optional[str] = Query(default=None),
    market_type: Optional[str] = Query(default=None),
    max_results: int = Query(default=100, ge=1, le=1000),
    sort: str = Query(default="FIRST_TO_START"),
    in_play_only: bool = Query(default=False),
) -> dict[str, Any]:
    filt: dict[str, Any] = {}
    if event_type_id:
        filt["eventTypeIds"] = [event_type_id]
    if country:
        filt["marketCountries"] = [country]
    if market_type:
        filt["marketTypeCodes"] = [market_type]
    if in_play_only:
        filt["inPlayOnly"] = True

    return await betfair_rest.list_market_catalogue(
        filter=filt,
        max_results=max_results,
        sort=sort,
        market_projection=[
            "RUNNER_DESCRIPTION",
            "RUNNER_METADATA",
            "EVENT",
            "MARKET_START_TIME",
            "MARKET_DESCRIPTION",
        ],
    )


# ─────────────────────────────────────────────────────────────────────────────
# LIVE-key writes
# ─────────────────────────────────────────────────────────────────────────────


def _upstream_error_envelope(exc: Exception) -> HTTPException:
    """Convert an upstream Betfair failure into a 502 with a structured detail.

    Live Betting Control needs a predictable error shape so it can
    decide whether to retry or escalate.
    """
    return HTTPException(
        status_code=502,
        detail={
            "ok": False,
            "dry_run": False,
            "upstream": "betfair",
            "error": exc.__class__.__name__,
            "message": str(exc),
        },
    )


@router.post("/orders/place", response_model=OrderResponse)
async def orders_place(req: PlaceOrdersRequest) -> dict[str, Any]:
    _check_orders_open()
    # Validate that each instruction has the right sub-order for its type.
    for ins in req.instructions:
        if ins.order_type == "LIMIT" and ins.limit_order is None:
            raise HTTPException(
                status_code=422,
                detail=f"selection_id {ins.selection_id}: LIMIT requires limit_order",
            )
        if ins.order_type == "LIMIT_ON_CLOSE" and ins.limit_on_close_order is None:
            raise HTTPException(
                status_code=422,
                detail=f"selection_id {ins.selection_id}: LIMIT_ON_CLOSE requires limit_on_close_order",
            )
        if ins.order_type == "MARKET_ON_CLOSE" and ins.market_on_close_order is None:
            raise HTTPException(
                status_code=422,
                detail=f"selection_id {ins.selection_id}: MARKET_ON_CLOSE requires market_on_close_order",
            )

    payload = req.model_dump(by_alias=True, exclude_none=True)
    try:
        return await betfair_rest.place_orders(payload)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _upstream_error_envelope(exc) from exc


@router.post("/orders/cancel", response_model=OrderResponse)
async def orders_cancel(req: CancelOrdersRequest) -> dict[str, Any]:
    _check_orders_open()
    payload = req.model_dump(by_alias=True, exclude_none=True)
    try:
        return await betfair_rest.cancel_orders(payload)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _upstream_error_envelope(exc) from exc


@router.post("/orders/replace", response_model=OrderResponse)
async def orders_replace(req: ReplaceOrdersRequest) -> dict[str, Any]:
    _check_orders_open()
    payload = req.model_dump(by_alias=True, exclude_none=True)
    try:
        return await betfair_rest.replace_orders(payload)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _upstream_error_envelope(exc) from exc
