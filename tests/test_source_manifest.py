"""Phase 4 — Source Manifest registration (Bible §21)."""
import json

import pytest

from core.config import replace_settings, reset_settings_for_test


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    # These tests verify GCS-flow with mocked storage.Client; clear
    # the kill-switch that conftest sets.
    monkeypatch.delenv("FSU1B_DISABLE_GCP_IO", raising=False)
    reset_settings_for_test()
    replace_settings(service_url="https://fsu1b-test.example.run.app")
    yield
    reset_settings_for_test()


class _FakeBlob:
    def __init__(self, existing_payload: dict | None = None):
        self._payload = existing_payload
        self.uploaded: str | None = None

    def exists(self):
        return self._payload is not None

    def download_as_text(self):
        if isinstance(self._payload, str):
            # For corrupted-blob test.
            return self._payload
        return json.dumps(self._payload)

    def upload_from_string(self, data, content_type=None):
        self.uploaded = data


class _FakeBucket:
    def __init__(self, blob):
        self._blob = blob

    def blob(self, name: str):
        return self._blob


class _FakeClient:
    def __init__(self, blob):
        self._bucket = _FakeBucket(blob)

    def bucket(self, name: str):
        return self._bucket


def _patch_storage(monkeypatch, blob):
    import google.cloud.storage  # type: ignore[import-not-found]
    monkeypatch.setattr(google.cloud.storage, "Client", lambda: _FakeClient(blob))


def test_register_creates_entry_when_manifest_missing(monkeypatch):
    blob = _FakeBlob(existing_payload=None)
    _patch_storage(monkeypatch, blob)

    from services.source_manifest import register

    entry = register()
    assert entry["name"] == "Betfair Exchange Gateway"
    assert entry["type"] == "live_stream"
    assert entry["url"] == "https://fsu1b-test.example.run.app"
    assert entry["status"] == "active"
    assert "horse_racing" in entry["sports"]
    assert "football" in entry["sports"]
    assert "tennis" in entry["sports"]

    manifest = json.loads(blob.uploaded)
    assert "fsu1b" in manifest
    assert manifest["fsu1b"] == entry


def test_register_merges_with_existing_manifest(monkeypatch):
    existing = {
        "fsu1a": {"name": "Historic Replay", "type": "historic_stream"},
        "fsu1c": {"name": "Racing API", "type": "racing_api"},
    }
    blob = _FakeBlob(existing_payload=existing)
    _patch_storage(monkeypatch, blob)

    from services.source_manifest import register

    register()

    manifest = json.loads(blob.uploaded)
    assert "fsu1a" in manifest  # other entries preserved
    assert "fsu1c" in manifest
    assert "fsu1b" in manifest
    assert manifest["fsu1a"]["name"] == "Historic Replay"  # untouched


def test_register_replaces_existing_fsu1b_entry(monkeypatch):
    existing = {
        "fsu1b": {
            "name": "old",
            "url": "https://old.example.run.app",
            "last_registered": "2026-01-01T00:00:00Z",
        },
    }
    blob = _FakeBlob(existing_payload=existing)
    _patch_storage(monkeypatch, blob)

    from services.source_manifest import register

    entry = register()
    assert entry["name"] == "Betfair Exchange Gateway"
    assert entry["url"] == "https://fsu1b-test.example.run.app"
    assert entry["last_registered"] != "2026-01-01T00:00:00Z"


def test_register_with_corrupted_blob_starts_fresh(monkeypatch):
    blob = _FakeBlob(existing_payload=None)
    # Override download_as_text to return invalid JSON.
    blob._payload = "<not json>"  # noqa: SLF001

    # exists() must return True for download to be attempted.
    def _exists():
        return True
    blob.exists = _exists

    _patch_storage(monkeypatch, blob)

    from services.source_manifest import register

    register()
    manifest = json.loads(blob.uploaded)
    assert list(manifest.keys()) == ["fsu1b"]


def test_register_best_effort_swallows_errors(monkeypatch):
    import google.cloud.storage  # type: ignore[import-not-found]

    def boom():
        raise RuntimeError("no ADC")

    monkeypatch.setattr(google.cloud.storage, "Client", boom)

    from services.source_manifest import register_best_effort

    result = register_best_effort()
    assert result is None  # swallowed


def test_endpoints_listed_match_actual_routes():
    """Cross-check: every endpoint advertised in the manifest exists on app."""
    from main import app
    from services.source_manifest import ENDPOINTS

    paths = {getattr(r, "path", None) for r in app.routes}
    for label, path in ENDPOINTS.items():
        # market_detail uses {id} but app uses {market_id} — both
        # represent the same parameterised route; compare prefixes.
        if "{" in path:
            prefix = path.split("{")[0]
            assert any(
                p and p.startswith(prefix) for p in paths
            ), f"manifest endpoint {label}={path} not in registered routes"
        else:
            assert path in paths, f"manifest endpoint {label}={path} not in registered routes"


def test_url_falls_back_to_empty_when_unset(monkeypatch):
    replace_settings(service_url="")
    blob = _FakeBlob(existing_payload=None)
    _patch_storage(monkeypatch, blob)

    from services.source_manifest import register

    entry = register()
    assert entry["url"] == ""
