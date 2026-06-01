"""Phase 4 — GCS-backed config persistence."""
import json
from unittest.mock import MagicMock

import pytest

from core.config import (
    get_settings,
    reset_settings_for_test,
    settings_to_dict,
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    # These tests verify the GCS code path with mocked storage.Client;
    # clear the kill-switch that conftest sets for the broader suite.
    monkeypatch.delenv("FSU1B_DISABLE_GCP_IO", raising=False)
    reset_settings_for_test()
    yield
    reset_settings_for_test()


class _FakeBlob:
    def __init__(self, existing_payload: dict | None = None):
        self._payload = existing_payload
        self.uploaded: str | None = None

    def exists(self):
        return self._payload is not None

    def download_as_text(self):
        return json.dumps(self._payload)

    def upload_from_string(self, data, content_type=None):
        self.uploaded = data


class _FakeBucket:
    def __init__(self, blob: _FakeBlob):
        self._blob = blob

    def blob(self, name: str):
        return self._blob


class _FakeClient:
    def __init__(self, blob: _FakeBlob):
        self._bucket = _FakeBucket(blob)

    def bucket(self, name: str):
        return self._bucket


def _patch_storage(monkeypatch, fake_blob: _FakeBlob):
    import google.cloud.storage  # type: ignore[import-not-found]
    monkeypatch.setattr(
        google.cloud.storage, "Client", lambda: _FakeClient(fake_blob),
    )


def test_load_from_existing_blob_applies_payload(monkeypatch):
    blob = _FakeBlob(existing_payload={
        "dry_run": True,
        "event_type_ids": ["7"],
        "service_url": "https://example.run.app",
    })
    _patch_storage(monkeypatch, blob)

    from core.gcs_config import load_config_from_gcs

    applied = load_config_from_gcs()
    assert applied["dry_run"] is True
    assert applied["event_type_ids"] == ["7"]
    s = get_settings()
    assert s.dry_run is True
    assert s.event_type_ids == ("7",)
    assert s.service_url == "https://example.run.app"


def test_load_with_missing_blob_writes_defaults(monkeypatch):
    blob = _FakeBlob(existing_payload=None)
    _patch_storage(monkeypatch, blob)

    from core.gcs_config import load_config_from_gcs

    applied = load_config_from_gcs()
    assert blob.uploaded is not None
    written = json.loads(blob.uploaded)
    assert written == settings_to_dict()
    assert applied == settings_to_dict()


def test_load_with_gcs_error_falls_back_to_in_memory(monkeypatch):
    import google.cloud.storage  # type: ignore[import-not-found]

    def boom():
        raise RuntimeError("no ADC")

    monkeypatch.setattr(google.cloud.storage, "Client", boom)

    from core.gcs_config import load_config_from_gcs

    applied = load_config_from_gcs()
    # Should not raise; returns current settings.
    assert applied == settings_to_dict()


def test_save_applies_and_uploads(monkeypatch):
    blob = _FakeBlob(existing_payload={})
    _patch_storage(monkeypatch, blob)

    from core.gcs_config import save_config_to_gcs

    ok = save_config_to_gcs({"dry_run": True, "auto_start": True})
    assert ok is True
    s = get_settings()
    assert s.dry_run is True
    assert s.auto_start is True

    # Uploaded JSON reflects the new settings.
    written = json.loads(blob.uploaded)
    assert written["dry_run"] is True
    assert written["auto_start"] is True


def test_save_returns_false_when_upload_fails(monkeypatch):
    import google.cloud.storage  # type: ignore[import-not-found]

    failing_blob = MagicMock()
    failing_blob.upload_from_string.side_effect = RuntimeError("permission denied")

    failing_bucket = MagicMock()
    failing_bucket.blob.return_value = failing_blob

    failing_client = MagicMock()
    failing_client.bucket.return_value = failing_bucket

    monkeypatch.setattr(google.cloud.storage, "Client", lambda: failing_client)

    from core.gcs_config import save_config_to_gcs

    ok = save_config_to_gcs({"dry_run": True})
    assert ok is False
    # In-memory change still happened (so operator can retry).
    s = get_settings()
    assert s.dry_run is True
