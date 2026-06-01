"""Phase 2 — admin endpoints reflect real LIVE-session/stream state."""


def test_admin_status_shape(client):
    r = client.get("/admin/status")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "fsu1b"
    assert body["phase"] == 4
    assert body["live_session"]["state"] == "not_started"
    assert body["delayed_session"]["state"] == "not_started"
    assert body["subscriptions"]["market_count"] == 0
    assert "stream" in body
    assert body["stream"]["running"] is False
    assert body["stream"]["watchdog_stale_threshold_s"] == 60
    assert body["stream"]["watchdog_check_interval_s"] == 30


def test_admin_config_returns_defaults(client):
    r = client.get("/admin/config")
    assert r.status_code == 200
    body = r.json()
    assert body["event_type_ids"] == ["7", "1", "2"]
    assert body["countries"] == ["GB", "IE"]
    assert body["market_types"] == ["WIN", "PLACE", "MATCH_ODDS"]
    assert body["stream_check_interval_s"] == 30
    assert body["stream_stale_threshold_s"] == 60
    assert body["dry_run"] is False
    assert body["auto_start"] is False
    assert body["log_level"] == "INFO"
    assert body["market_hours_start_utc"] == "08:00"
    assert body["market_hours_end_utc"] == "23:00"


def test_admin_config_put_updates_and_persists(client, monkeypatch):
    """Phase 4: PUT applies in-memory AND persists to GCS."""
    # Patch the GCS write helper so we don't try to reach GCS in tests.
    persisted = []

    def fake_save(payload):
        # Real helper also calls apply_dict; we mirror that here so the
        # in-memory change still happens.
        from core.config import apply_dict
        apply_dict(payload)
        persisted.append(payload)
        return True

    from services import admin as admin_module
    monkeypatch.setattr(admin_module, "save_config_to_gcs", fake_save)

    r = client.put("/admin/config", json={"dry_run": True})
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert persisted == [{"dry_run": True}]

    # Reset for other tests.
    client.put("/admin/config", json={"dry_run": False})


def test_admin_config_put_returns_502_on_persistence_failure(client, monkeypatch):
    def fake_save(payload):
        from core.config import apply_dict
        apply_dict(payload)
        return False  # GCS write failed

    from services import admin as admin_module
    monkeypatch.setattr(admin_module, "save_config_to_gcs", fake_save)

    r = client.put("/admin/config", json={"dry_run": True})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["ok"] is False
    assert detail["applied_in_memory"] is True
    assert detail["persisted_to_gcs"] is False

    # Reset.
    monkeypatch.undo()
    from core.config import apply_dict
    apply_dict({"dry_run": False})


def test_admin_stats_shape(client):
    r = client.get("/admin/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["messages_per_s"] == 0.0
    assert body["mcm_count"] == 0
    assert body["mcm_count_by_event_type"] == {}
    assert body["reconnect_count"] == 0
    assert body["markets_subscribed"] == 0
    assert "subscribers_by_channel" in body
    # Phase 4.1: real per-sport + per-endpoint signals.
    assert "last_message_at_by_sport" in body
    assert "last_call_at_by_endpoint" in body
    assert "call_count_by_endpoint" in body


def test_admin_stats_records_real_endpoint_calls(client):
    """Hitting an endpoint must register in stats (no placeholders)."""
    # Drive some real traffic.
    client.get("/admin/config")
    client.get("/admin/status")

    r = client.get("/admin/stats")
    body = r.json()
    # The middleware records the path of every non-observability call.
    assert "/admin/config" in body["last_call_at_by_endpoint"]
    assert "/admin/status" in body["last_call_at_by_endpoint"]
    assert body["call_count_by_endpoint"]["/admin/config"] >= 1


def test_admin_stats_excludes_observability_paths(client):
    """Liveness/observability paths must NOT appear in per-endpoint stats."""
    client.get("/health")
    client.get("/ready")
    client.get("/metrics")
    r = client.get("/admin/stats")
    body = r.json()
    for p in ("/health", "/ready", "/metrics", "/info", "/status"):
        assert p not in body["last_call_at_by_endpoint"], f"{p} should be excluded"


def test_admin_stats_last_message_by_sport_after_mcm():
    """When a market_change arrives, last_message_at_by_sport stamps the sport."""
    import asyncio
    from core.state import app_state, reset_state_for_test
    from services.market_cache import market_cache

    reset_state_for_test()

    async def run():
        await market_cache.apply_mcm({
            "mc": [{"id": "1.test", "img": True,
                     "marketDefinition": {"eventTypeId": "7"}}]
        })
        # Simulate what stream_client does when an mcm arrives:
        app_state.note_message("7")

    asyncio.run(run())
    assert "7" in app_state.last_message_at_by_event_type
    assert app_state.last_message_at_by_event_type["7"] is not None
    asyncio.run(market_cache.reset_for_test())
    reset_state_for_test()


def test_admin_activity_records_boot_events(client):
    """Phase 4: lifespan publishes gateway_started which writes one activity entry."""
    r = client.get("/admin/activity")
    assert r.status_code == 200
    events = r.json()["events"]
    # Expect at least one entry from the lifespan boot publish.
    kinds = {e["kind"] for e in events}
    assert any(k.startswith("event:gateway_") for k in kinds), (
        f"no infra event in activity feed; kinds={kinds!r}"
    )


def test_admin_control_pause_now_wired(client):
    """Phase 3: pause wires through to app_state.orders_paused."""
    from core.state import app_state

    try:
        r = client.post("/admin/control/pause")
        assert r.status_code == 200
        body = r.json()
        assert body["accepted"] is True
        assert body["executed"] is True
        assert app_state.orders_paused is True
    finally:
        # Reset so other tests aren't affected.
        client.post("/admin/control/resume")


def test_admin_control_test_verb(client):
    r = client.post("/admin/control/test")
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["executed"] is True


def test_admin_control_rejects_unknown_verb(client):
    r = client.post("/admin/control/blow_up")
    assert r.status_code == 422


def test_admin_control_reconnect_rejected_when_idle(client):
    """reconnect_stream requires the session to be running."""
    r = client.post("/admin/control/reconnect_stream")
    assert r.status_code == 409


def test_admin_events_sse_opens(client):
    with client.stream("GET", "/admin/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
