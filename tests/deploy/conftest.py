"""Shared test fixtures for the deploy-server test suite."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from robotsix_deploy.main import create_app


@pytest.fixture
def client() -> TestClient:
    """Return a FastAPI TestClient for the deploy server."""
    app = create_app()
    return TestClient(app)
