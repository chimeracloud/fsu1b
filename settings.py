"""Recorder configuration — minimal dataclasses.

The recorder is intentionally constrained: it ONLY records what
Betfair sends. No calculations, no derived values. Settings here
control what to subscribe to and when to roll over the daily file,
nothing else.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass
class RecorderSettings:
    """Operator-tunable recorder configuration.

    Persisted at ``gs://chiops-betfair-recording/settings/current.json``
    (Phase 2). The shell uses dataclass defaults only.
    """

    # Market filter — what to subscribe to
    event_type_ids: list[str] = field(default_factory=lambda: ["7"])  # 7 = Horse Racing
    market_countries: list[str] = field(default_factory=lambda: ["GB", "IE"])
    market_type_codes: list[str] = field(default_factory=lambda: ["WIN"])

    # Buffer + flush behaviour
    flush_interval_seconds: int = 60
    flush_threshold_lines: int = 1000

    # Daily rollover (UTC hour at which a new file begins)
    rollover_hour_utc: int = 12

    # Auto-start recording on container boot. FALSE — hardened after the
    # 2026-05-17 incident. FSU1B currently shares the SAME Betfair
    # account/app key as the live engine (CLE V2). If the recorder
    # auto-resumes on a stray Cloud Run restart while the engine is
    # trading, it re-grabs the shared Betfair session and the live
    # engine's session is invalidated (INVALID_SESSION_INFORMATION) —
    # i.e. it takes the money engine down. Until FSU1B has its own
    # Betfair app key (or the single-gateway redesign lands), the
    # recorder must be started EXPLICITLY via POST /admin/control/start
    # so a container restart can never silently take out live betting.
    auto_start: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecorderSettings":
        kwargs = {}
        for f in fields(cls):
            if f.name in data:
                kwargs[f.name] = data[f.name]
        return cls(**kwargs)
