"""Phase 1 — verify the standard observability shell is wired."""


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready_is_true_in_phase_1(client):
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["phase"] == 1


def test_info_identifies_service(client):
    r = client.get("/info")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "fsu1b"
    assert body["phase"] == 1
    assert "version" in body


def test_metrics_is_prometheus_plaintext(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "fsu1b_uptime_seconds" in r.text


def test_status_has_uptime_and_now(client):
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "fsu1b"
    assert body["phase"] == 1
    assert "uptime_s" in body
    assert "now" in body
