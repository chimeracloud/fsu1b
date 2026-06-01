"""Phase 4 — `POST /admin/control/reregister_source`."""
import pytest

from core.config import reset_settings_for_test
from core.state import reset_state_for_test


@pytest.fixture(autouse=True)
def _isolated():
    reset_settings_for_test()
    reset_state_for_test()
    yield
    reset_settings_for_test()
    reset_state_for_test()


def test_reregister_source_success(client, monkeypatch):
    captured = {}

    def fake_register():
        captured["called"] = True
        return {"name": "Betfair Exchange Gateway", "url": "https://x.run.app"}

    from services import source_manifest
    monkeypatch.setattr(source_manifest, "register", fake_register)

    r = client.post("/admin/control/reregister_source")
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["executed"] is True
    assert captured.get("called") is True


def test_reregister_source_failure_reports_unexecuted(client, monkeypatch):
    def boom():
        raise RuntimeError("no manifest bucket access")

    from services import source_manifest
    monkeypatch.setattr(source_manifest, "register", boom)

    r = client.post("/admin/control/reregister_source")
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["executed"] is False
    assert "no manifest bucket access" in body["note"]
