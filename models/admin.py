"""
Pydantic v2 response models for Set 1 admin endpoints.

Phase 1 defines the shapes. Values are placeholders. The shapes do
not change in later phases — that's the point of shell-first.
"""
from datetime import datetime
from typing import Literal

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
    last_clk: str | None = Field(
        default=None,
        description="Stream cursor token (LIVE-key session only).",
    )
    last_error: str | None = None


class SubscriptionsSummary(BaseModel):
    market_count: int = 0
    by_sport: dict[str, int] = Field(
        default_factory=dict,
        description="Subscribed-market count keyed by Betfair eventTypeId.",
    )


class AdminStatusResponse(BaseModel):
    service: str
    version: str
    phase: int
    live_session: SessionState
    delayed_session: SessionState
    subscriptions: SubscriptionsSummary
    now: datetime


class AdminConfigResponse(BaseModel):
    event_type_ids: list[str]
    countries: list[str]
    market_types: list[str]
    stream_check_interval_s: int
    stream_stale_threshold_s: int
    dry_run: bool


class AdminConfigUpdate(BaseModel):
    event_type_ids: list[str] | None = None
    countries: list[str] | None = None
    market_types: list[str] | None = None
    stream_check_interval_s: int | None = None
    stream_stale_threshold_s: int | None = None
    dry_run: bool | None = None


class AdminStatsResponse(BaseModel):
    messages_per_s: float
    bytes_in_per_s: float
    bytes_out_per_s: float
    orders_placed: int
    orders_cancelled: int
    rest_errors: int
    stream_reconnects: int


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
