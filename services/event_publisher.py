"""
Event envelope publisher (Bible §20).

FSU1B emits *infrastructure* events only — never business events.
Order placement / settlement events belong to Live Betting Control
FSU (ADR-018).

Envelope (locked by Bible §20):

    {
      "envelope": {
        "source":     "fsu1b",
        "event_type": "gateway_started",
        "timestamp":  "<UTC iso>",
        "version":    "1.0"
      },
      "payload": { ...event-specific... }
    }

Seven event types Phase 4 fires:

  gateway_started            — service up, ready to accept commands
  gateway_stopped            — graceful stop
  gateway_session_dropped    — LIVE or DELAYED session lost
  gateway_session_recovered  — session re-established after drop
  gateway_stream_stale       — watchdog tripped; reconnect imminent
  gateway_reconnected        — stream reconnected after a drop
  gateway_daily_summary      — end-of-day stats (markets, mcm, errors)

Stub fallback:
  If google-cloud-pubsub is not available or the topic publish fails,
  the envelope is logged to stdout (so Cloud Logging still captures
  it) and broadcast to the admin/events SSE channel so operators can
  see it on the portal even when Pub/Sub is misconfigured.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Literal

from core.config import get_settings
from core.state import app_state

logger = logging.getLogger(__name__)


def _disabled() -> bool:
    return bool(os.environ.get("FSU1B_DISABLE_GCP_IO"))

ENVELOPE_VERSION = "1.0"

EventType = Literal[
    "gateway_started",
    "gateway_stopped",
    "gateway_session_dropped",
    "gateway_session_recovered",
    "gateway_stream_stale",
    "gateway_reconnected",
    "gateway_daily_summary",
]

VALID_EVENT_TYPES: set[str] = {
    "gateway_started",
    "gateway_stopped",
    "gateway_session_dropped",
    "gateway_session_recovered",
    "gateway_stream_stale",
    "gateway_reconnected",
    "gateway_daily_summary",
}

# Module-level Pub/Sub publisher cache. Lazy-init so unit tests don't
# need google-cloud-pubsub installed.
_publisher = None
_topic_path: str | None = None
_publisher_disabled = False  # Set True after a failed init; stub mode.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_envelope(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(
            f"unknown event_type={event_type!r}; allowed={sorted(VALID_EVENT_TYPES)}"
        )
    return {
        "envelope": {
            "source": "fsu1b",
            "event_type": event_type,
            "timestamp": _now_iso(),
            "version": ENVELOPE_VERSION,
        },
        "payload": payload,
    }


def _get_publisher():
    """Lazy-init the Pub/Sub publisher. Returns (publisher, topic_path) or None."""
    global _publisher, _topic_path, _publisher_disabled
    if _disabled() or _publisher_disabled:
        return None
    if _publisher is not None and _topic_path is not None:
        return _publisher, _topic_path
    try:
        from google.cloud import pubsub_v1  # type: ignore[import-not-found]

        settings = get_settings()
        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(settings.gcp_project, settings.events_topic)
        _publisher = publisher
        _topic_path = topic_path
        logger.info("Pub/Sub publisher ready: %s", topic_path)
        return _publisher, _topic_path
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Pub/Sub publisher init failed (%s) — switching to stub mode.", exc,
        )
        _publisher_disabled = True
        return None


async def publish(event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Publish an envelope. Returns the envelope that was sent (for logging/tests).

    Path A (Pub/Sub ok): publish via google-cloud-pubsub.
    Path B (stub): log + push to /admin/events SSE channel.

    Either way, the activity feed gets a row so /admin/activity shows
    every infrastructure event the gateway has emitted.
    """
    env = build_envelope(event_type, payload or {})
    app_state.add_activity(f"event:{event_type}", _short(payload))

    pub = _get_publisher()
    if pub is None:
        # Stub mode: log + SSE broadcast.
        logger.info("[event-stub] %s", json.dumps(env, default=str))
        await app_state.broadcast("all", {"event": "infra_event", **env})
        return env

    publisher, topic_path = pub
    data = json.dumps(env, default=str).encode("utf-8")
    try:
        future = publisher.publish(
            topic_path,
            data,
            event_type=event_type,
            source="fsu1b",
        )
        # publish() is sync; .result() is blocking — dispatch to executor.
        loop = asyncio.get_running_loop()
        message_id = await loop.run_in_executor(None, future.result, 10)
        logger.info(
            "Published event_type=%s topic=%s message_id=%s",
            event_type, topic_path, message_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pub/Sub publish failed (%s) — logging stub instead.", exc)
        logger.info("[event-stub-on-failure] %s", json.dumps(env, default=str))
    # Always SSE-broadcast the event so the admin dashboard sees it.
    await app_state.broadcast("all", {"event": "infra_event", **env})
    return env


def _short(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    s = json.dumps(payload, default=str, separators=(",", ":"))
    return s if len(s) <= 200 else s[:197] + "..."


def reset_publisher_for_test() -> None:
    """Test-only — drop the cached publisher so a fresh init runs."""
    global _publisher, _topic_path, _publisher_disabled
    _publisher = None
    _topic_path = None
    _publisher_disabled = False
