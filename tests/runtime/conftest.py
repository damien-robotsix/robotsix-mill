"""Shared fixtures for runtime HTTP tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.runtime.api import create_app


@pytest.fixture
def client(settings, repos_registry):
    """Reusable TestClient wired to the single-repo test app."""
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c
