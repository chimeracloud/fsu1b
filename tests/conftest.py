"""
Shared pytest fixtures.

The lifespan context manager (in `main.py`) is what populates
`app.state.settings`. Using `TestClient` as a context manager runs
that lifespan — so every test gets a fully-initialised app.
"""
import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
