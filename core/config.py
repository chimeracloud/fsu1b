"""
Configuration.

Phase 1: in-memory defaults only. Phase 4 swaps these for GCS-backed
config per CHI-POL-006 (portal-editable config, no env vars for
settings). Credentials never live here — Secret Manager only
(CHI-POL-003).
"""
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    # Identity.
    service_name: str = "fsu1b"
    region: str = "europe-west2"
    gcp_project: str = "chiops"

    # Subscription filter — applied at stream content phase (Phase 2).
    # SC go-ahead: subscribe to horse racing (7), football (1), tennis (2).
    event_type_ids: tuple[str, ...] = ("7", "1", "2")
    countries: tuple[str, ...] = ("GB", "IE")
    market_types: tuple[str, ...] = ("WIN",)

    # Watchdog (SC go-ahead Phase 2).
    stream_check_interval_s: int = 30
    stream_stale_threshold_s: int = 60

    # Operational flags.
    dry_run: bool = False  # Phase 3: when True, log payload, don't call Betfair.


@lru_cache
def get_settings() -> Settings:
    return Settings()
