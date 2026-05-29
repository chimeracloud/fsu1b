"""Phase 1 — verify the Set 1 admin shell is wired and shapes are stable."""


def test_admin_status_shape(client):
    r = client.get("/admin/status")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "fsu1b"
    assert body["phase"] == 1
    assert body["live_session"]["state"] == "not_started"
    assert body["delayed_session"]["state"] == "not_started"
    assert body["subscriptions"]["market_count"] == 0


def test_admin_config_returns_defaults(client):
    r = client.get("/admin/config")
    assert r.status_code == 200
    body = r.json()
    assert body["event_type_ids"] == ["7", "1", "2"]
    assert body["countries"] == ["GB", "IE"]
    assert body["market_types"] == ["WIN"]
    assert body["stream_check_interval_s"] == 30
    assert body["stream_stale_threshold_s"] == 60
    assert body["dry_run"] is False


def test_admin_config_put_accepts_shape(client):
    r = client.put("/admin/config", json={"dry_run": True})
    assert r.status_code == 200
    # Phase 1: persistence not wired → returns current.
    assert r.json()["dry_run"] is False


def test_admin_stats_zero_in_phase_1(client):
    r = client.get("/admin/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["messages_per_s"] == 0.0
    assert body["orders_placed"] == 0
    assert body["rest_errors"] == 0


def test_admin_activity_empty_in_phase_1(client):
    r = client.get("/admin/activity")
    assert r.status_code == 200
    assert r.json() == {"events": []}


def test_admin_control_accepts_known_verb(client):
    r = client.post("/admin/control/start")
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "start"
    assert body["accepted"] is True
    assert body["executed"] is False


def test_admin_control_rejects_unknown_verb(client):
    r = client.post("/admin/control/blow_up")
    assert r.status_code == 422


def test_admin_events_sse_opens(client):
    with client.stream("GET", "/admin/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
