"""
Shared pytest fixtures.

The lifespan context manager (in `main.py`) is what populates
`app.state.settings`. Using `TestClient` as a context manager runs
that lifespan — so every test gets a fully-initialised app.

Test-mode safety: `FSU1B_DISABLE_GCP_IO` is set at module load (before
any FSU1B module is imported) so the lifespan's GCS/Pub/Sub calls
short-circuit to in-memory defaults / stub mode. Without this, every
test would hit real GCP services, slow down 100×, and risk writing to
production buckets.
"""
import os

# CRITICAL: set BEFORE anything imports our modules.
os.environ.setdefault("FSU1B_DISABLE_GCP_IO", "1")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_subscription_guard():
    """The guard latches its warning and throttles by wall clock.

    Both are module-level, so without a reset one test's warning would
    suppress the next test's.
    """
    from services import subscription_guard

    subscription_guard.reset_for_test()
    yield
    subscription_guard.reset_for_test()
