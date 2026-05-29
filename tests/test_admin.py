"""Phase 2 — admin endpoints reflect real LIVE-session/stream state."""


def test_admin_status_shape(client):
    r = client.get("/admin/status")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "fsu1b"
    assert body["phase"] == 2
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


def test_admin_config_put_updates_in_memory(client):
    """Phase 2: PUT updates the in-memory settings; GCS persistence is Phase 4."""
    r = client.put("/admin/config", json={"dry_run": True})
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    # Reset for other tests.
    client.put("/admin/config", json={"dry_run": False})


def test_admin_stats_phase_2_shape(client):
    r = client.get("/admin/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["messages_per_s"] == 0.0
    assert body["mcm_count"] == 0
    assert body["mcm_count_by_event_type"] == {}
    assert body["reconnect_count"] == 0
    assert body["markets_subscribed"] == 0
    assert "subscribers_by_channel" in body


def test_admin_activity_empty_at_boot(client):
    r = client.get("/admin/activity")
    assert r.status_code == 200
    # Activity buffer may contain auto-start log if auto_start=True.
    # With default False, expect empty.
    assert r.json() == {"events": []}


def test_admin_control_pause_accepted_unwired(client):
    """pause/resume/relogin_rest are accepted but Phase 3 wires the action."""
    r = client.post("/admin/control/pause")
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["executed"] is False


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
