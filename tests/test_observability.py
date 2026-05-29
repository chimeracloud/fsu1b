"""Phase 3 — observability shell with stream-freshness-aware /ready."""

EXPECTED_PHASE = 3


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready_idle_when_session_not_started(client):
    """auto_start=False default → /ready 200 with mode=idle."""
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["mode"] == "idle"
    assert body["phase"] == EXPECTED_PHASE


def test_info_identifies_service(client):
    r = client.get("/info")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "fsu1b"
    assert body["phase"] == EXPECTED_PHASE
    assert "version" in body


def test_metrics_is_prometheus_plaintext(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "fsu1b_uptime_seconds" in r.text
    assert "fsu1b_mcm_total" in r.text
    assert "fsu1b_reconnects_total" in r.text


def test_status_has_uptime_and_now(client):
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "fsu1b"
    assert body["phase"] == EXPECTED_PHASE
    assert "uptime_s" in body
    assert "now" in body
    assert "stream_status" in body
    assert "mcm_count" in body
