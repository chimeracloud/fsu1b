"""
Pydantic v2 response models for Set 1 admin endpoints.

Phase 2: shapes reflect real LIVE-session + stream state. DELAYED
session shape is in place but its values stay `not_started` until
Phase 3.
"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SessionStateLiteral = Literal[
    "not_started",
    "logging_in",
    "active",
    "reconnecting",
    "failed",
]


class SessionState(BaseModel):
    state: SessionStateLiteral
    last_login: datetime | None = None
    last_keepalive: datetime | None = None
    last_clk: str | None = Field(
        default=None,
        description="Stream resub cursor (LIVE session only).",
    )
    last_error: str | None = None


class SubscriptionsSummary(BaseModel):
    market_count: int = 0
    by_sport: dict[str, int] = Field(
        default_factory=dict,
        description="Active-market count keyed by sport label.",
    )


class AdminStatusResponse(BaseModel):
    service: str
    version: str
    phase: int
    live_session: SessionState
    delayed_session: SessionState
    subscriptions: SubscriptionsSummary
    stream: dict[str, Any]
    now: datetime


class AdminConfigResponse(BaseModel):
    event_type_ids: list[str]
    countries: list[str]
    market_types: list[str]
    stream_check_interval_s: int
    stream_stale_threshold_s: int
    dry_run: bool
    auto_start: bool


class AdminConfigUpdate(BaseModel):
    event_type_ids: list[str] | None = None
    countries: list[str] | None = None
    market_types: list[str] | None = None
    stream_check_interval_s: int | None = None
    stream_stale_threshold_s: int | None = None
    dry_run: bool | None = None
    auto_start: bool | None = None


class AdminStatsResponse(BaseModel):
    messages_per_s: float
    mcm_count: int
    mcm_count_by_event_type: dict[str, int]
    reconnect_count: int
    markets_subscribed: int
    subscribers_by_channel: dict[str, int]


class ActivityEvent(BaseModel):
    ts: datetime
    kind: str
    detail: str


class AdminActivityResponse(BaseModel):
    events: list[ActivityEvent]


class ControlActionResponse(BaseModel):
    action: str
    accepted: bool
    executed: bool
    note: str
    at: datetime
