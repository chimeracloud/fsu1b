"""
Singleton application state.

Holds the runtime status of the stream + REST sessions, the in-memory
market cache, and the per-sport SSE pub/sub.

This module is dependency-free (apart from stdlib) so any other module
can import it without cycles.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

UTC = timezone.utc

StreamStatus = Literal[
    "disconnected", "connecting", "connected", "reconnecting", "failed"
]
SessionStatus = Literal[
    "not_started", "logging_in", "active", "reconnecting", "failed"
]


@dataclass
class SessionInfo:
    state: SessionStatus = "not_started"
    last_login: Optional[datetime] = None
    last_keepalive: Optional[datetime] = None
    last_error: Optional[str] = None


class AppState:
    """Singleton, accessed via the module-level ``app_state``."""

    def __init__(self) -> None:
        # Session-level — two sessions, Phase 2 only uses LIVE.
        self.live_session = SessionInfo()
        self.delayed_session = SessionInfo()  # Phase 3.

        # Stream connection (LIVE key).
        self.stream_status: StreamStatus = "disconnected"
        self.connection_id: Optional[str] = None
        self.stream_latency_503: bool = False
        self.last_message_at: Optional[datetime] = None
        self.reconnect_count: int = 0
        self.started_at: datetime = datetime.now(UTC)

        # Order kill-switch (admin/control/pause|resume).
        # When True, write-side REST endpoints (orders/place|cancel|replace)
        # return 503 without touching Betfair. Stream + read REST unaffected.
        self.orders_paused: bool = False

        # Counters for /admin/stats.
        self.mcm_count: int = 0
        self.mcm_count_by_event_type: dict[str, int] = {}
        self._msg_ts: deque[float] = deque(maxlen=4096)

        # Per-sport last-message timestamps — drives DATA IN LEDs without
        # frontend dead-reckoning. Keyed by eventTypeId ("7","1","2").
        self.last_message_at_by_event_type: dict[str, datetime] = {}

        # Per-endpoint last-call timestamps — drives DATA OUT LEDs from
        # real consumer activity (recorded by the request middleware in
        # main.py). Keyed by URL path.
        self.last_call_at_by_endpoint: dict[str, datetime] = {}
        self.call_count_by_endpoint: dict[str, int] = {}

        # Activity ring buffer for /admin/activity.
        self._activity: deque[dict] = deque(maxlen=200)

        # Per-sport SSE pub/sub. Key: sport name ("horse-racing", "football",
        # "tennis", "all"). Value: list of asyncio.Queue subscribers.
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._sub_lock = asyncio.Lock()

    # ── Activity feed ─────────────────────────────────────────────────────

    def add_activity(self, kind: str, detail: str) -> None:
        self._activity.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "kind": kind,
                "detail": detail,
            }
        )

    def recent_activity(self, limit: int = 100) -> list[dict]:
        return list(self._activity)[-limit:]

    # ── Stream freshness ─────────────────────────────────────────────────

    def stream_is_fresh(self, stale_threshold_s: int) -> bool:
        """True if a stream message has arrived within the threshold."""
        if self.last_message_at is None:
            return False
        age = (datetime.now(UTC) - self.last_message_at).total_seconds()
        return age <= stale_threshold_s

    def stream_age_s(self) -> Optional[float]:
        if self.last_message_at is None:
            return None
        return (datetime.now(UTC) - self.last_message_at).total_seconds()

    # ── Rate (last 60s) ──────────────────────────────────────────────────

    def note_message(self, event_type_id: Optional[str]) -> None:
        import time as _t
        now = datetime.now(UTC)
        self.mcm_count += 1
        if event_type_id:
            self.mcm_count_by_event_type[event_type_id] = (
                self.mcm_count_by_event_type.get(event_type_id, 0) + 1
            )
            self.last_message_at_by_event_type[event_type_id] = now
        self._msg_ts.append(_t.time())
        self.last_message_at = now

    def note_endpoint_call(self, path: str) -> None:
        """Record an inbound HTTP call. Drives OUT-side LEDs."""
        self.last_call_at_by_endpoint[path] = datetime.now(UTC)
        self.call_count_by_endpoint[path] = (
            self.call_count_by_endpoint.get(path, 0) + 1
        )

    def messages_per_s_recent(self, window_s: float = 60.0) -> float:
        import time as _t
        now = _t.time()
        recent = sum(1 for ts in self._msg_ts if (now - ts) <= window_s)
        return recent / window_s

    # ── SSE pub/sub ──────────────────────────────────────────────────────

    async def subscribe(self, channel: str) -> asyncio.Queue:
        """Subscribe to a per-sport channel or 'all'."""
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        async with self._sub_lock:
            self._subscribers.setdefault(channel, []).append(q)
        return q

    async def unsubscribe(self, channel: str, q: asyncio.Queue) -> None:
        async with self._sub_lock:
            subs = self._subscribers.get(channel, [])
            self._subscribers[channel] = [s for s in subs if s is not q]

    async def broadcast(self, channel: str, event: dict) -> None:
        """Publish to one sport channel; mirrors to the 'all' channel."""
        async with self._sub_lock:
            for ch in (channel, "all"):
                for q in self._subscribers.get(ch, []):
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        # Drop rather than block — slow consumer.
                        pass

    def subscriber_count(self) -> dict[str, int]:
        return {ch: len(subs) for ch, subs in self._subscribers.items()}


# Module-level singleton.
app_state = AppState()


def reset_state_for_test() -> None:
    """Test-only — reset the singleton's fields in place.

    Critical: must mutate the *existing* object rather than rebind the
    module-level name. Other modules import `app_state` by reference;
    rebinding here wouldn't propagate, and stale flags (e.g.
    `orders_paused`) would leak between tests.
    """
    fresh = AppState()
    app_state.__dict__.update(fresh.__dict__)
