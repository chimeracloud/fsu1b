"""
Pydantic v2 response models for Set 1 admin endpoints.

Phase 2: shapes reflect real LIVE-session + stream state. DELAYED
session shape is in place but its values stay `not_started` until
Phase 3.
"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    market_count: int = Field(
        default=0,
        description=(
            "Live count of markets this subscription is carrying. Proxied "
            "from the market cache; may briefly overstate, because CLOSED "
            "markets stay cached until the 300s maintenance sweep."
        ),
    )
    by_sport: dict[str, int] = Field(
        default_factory=dict,
        description="Active-market count keyed by sport label.",
    )
    limit: int = Field(
        default=0,
        description=(
            "Configured Betfair subscription ceiling (Settings."
            "subscription_limit). Chimera's allocation is 1,000 markets "
            "per session as of 2026-06-08."
        ),
    )
    warn_at: int = Field(
        default=0,
        description="Market count at which the gateway warns (limit x warn_pct).",
    )
    pct_of_limit: float = 0.0
    at_warning_level: bool = False


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
    subscription_limit: int
    subscription_warn_pct: float
    dry_run: bool
    auto_start: bool
    log_level: str
    market_hours_start_utc: str
    market_hours_end_utc: str


class AdminConfigUpdate(BaseModel):
    event_type_ids: list[str] | None = None
    countries: list[str] | None = None
    market_types: list[str] | None = None
    stream_check_interval_s: int | None = None
    stream_stale_threshold_s: int | None = None
    subscription_limit: int | None = None
    subscription_warn_pct: float | None = None
    dry_run: bool | None = None
    auto_start: bool | None = None
    log_level: str | None = None
    market_hours_start_utc: str | None = None
    market_hours_end_utc: str | None = None


class AdminStatsResponse(BaseModel):
    messages_per_s: float
    mcm_count: int
    mcm_count_by_event_type: dict[str, int]
    last_message_at_by_sport: dict[str, datetime | None] = Field(
        default_factory=dict,
        description=(
            "Per-sport timestamps driven by real SSE message arrival. Keys "
            "are sport labels (horse-racing / football / tennis). Null means "
            "no message has arrived for that sport yet — the GUI shows the "
            "grey-hollow LED in that case."
        ),
    )
    last_call_at_by_endpoint: dict[str, datetime | None] = Field(
        default_factory=dict,
        description=(
            "Per-endpoint last inbound call timestamp. Drives DATA OUT LEDs "
            "from real consumer activity. Observability paths "
            "(/health, /ready, /metrics, /info, /status) are excluded."
        ),
    )
    call_count_by_endpoint: dict[str, int] = Field(default_factory=dict)
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
