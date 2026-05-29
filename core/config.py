"""
Configuration.

Phase 2: in-memory dataclass with mutable runtime state behind a
get/replace API. Phase 4 will back this onto GCS per CHI-POL-006
(portal-editable, no env vars for settings). Credentials never live
here — Secret Manager only (CHI-POL-003).

Sport-id mapping (Betfair eventTypeId → Chimera sport label):
  "7" → horse-racing
  "1" → football      (Betfair: "Soccer")
  "2" → tennis

SC go-ahead Phase 2 subscribes to all three from day one.
"""
from dataclasses import dataclass, field, replace
from threading import RLock


# Public mapping — used by stream routing and admin display.
SPORT_BY_EVENT_TYPE: dict[str, str] = {
    "7": "horse-racing",
    "1": "football",
    "2": "tennis",
}
EVENT_TYPE_BY_SPORT: dict[str, str] = {v: k for k, v in SPORT_BY_EVENT_TYPE.items()}


@dataclass(frozen=True)
class SecretRefs:
    """Secret Manager secret NAMES (not values) for credentials.

    Kept as config so the app-key secret can be renamed without a
    redeploy. Defaults match the naming convention locked in the
    Bible's Betfair App Key Naming Convention.

    Phase 2 uses only the LIVE key. Phase 3 wires DELAYED.
    """

    username: str = "betfair-username"
    password: str = "betfair-password"
    cert_pem: str = "betfair-cert-pem"
    key_pem: str = "betfair-key-pem"
    live_app_key: str = "betfair-app-key-live"
    delayed_app_key: str = "betfair-app-key-delayed"  # Phase 3 only


@dataclass(frozen=True)
class Settings:
    # Identity.
    service_name: str = "fsu1b"
    region: str = "europe-west2"
    gcp_project: str = "chiops"

    # Subscription filter — applied at stream subscribe.
    # SC go-ahead: subscribe to horse racing (7), football (1), tennis (2).
    event_type_ids: tuple[str, ...] = ("7", "1", "2")
    countries: tuple[str, ...] = ("GB", "IE")
    market_types: tuple[str, ...] = ("WIN", "PLACE", "MATCH_ODDS")

    # Stream protocol.
    stream_host: str = "stream-api.betfair.com"
    stream_port: int = 443
    heartbeat_ms: int = 5000
    reconnect_max_backoff_s: int = 300
    session_keepalive_hours: int = 4  # well under Betfair's 24h limit

    # Market data fields (Betfair ESA spec).
    market_data_fields: tuple[str, ...] = (
        "EX_BEST_OFFERS",
        "EX_MARKET_DEF",
        "EX_TRADED",
        "EX_TRADED_VOL",
        "EX_LTP",
        "SP_PROJECTED",
    )

    # Watchdog (SC go-ahead Phase 2).
    stream_check_interval_s: int = 30
    stream_stale_threshold_s: int = 60

    # Lifecycle.
    auto_start: bool = False  # Post 2026-05-17 hardening — operator starts explicitly.

    # Operational flags.
    dry_run: bool = False  # Phase 3: when True, log payload, don't call Betfair.

    # Secret Manager refs.
    secrets: SecretRefs = field(default_factory=SecretRefs)


_lock = RLock()
_current: Settings = Settings()


def get_settings() -> Settings:
    with _lock:
        return _current


def replace_settings(**changes) -> Settings:
    """Replace specified fields. Returns the new Settings."""
    global _current
    with _lock:
        # Allow nested replace for `secrets` keyword.
        if "secrets" in changes and isinstance(changes["secrets"], dict):
            changes["secrets"] = replace(_current.secrets, **changes["secrets"])
        _current = replace(_current, **changes)
        return _current


def reset_settings_for_test() -> None:
    """Test-only — restore defaults so tests don't leak state."""
    global _current
    with _lock:
        _current = Settings()
