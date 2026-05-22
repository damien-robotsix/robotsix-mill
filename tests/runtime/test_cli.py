import pytest

from robotsix_mill.cli import main
from robotsix_mill.core.states import State


def test_approve_success(settings, service):
    """CLI `ticket approve <id>` exits 0 on success with human-readable output."""
    t = service.create("Approve me via CLI")
    service.transition(t.id, State.HUMAN_ISSUE_APPROVAL, note="refined")

    # Override api_url so the CLI hits the test server instead of localhost.
    # We can't easily run the full server in a CLI test, so we test the
    # client-side logic by mocking httpx. But the CLI uses a real HTTP
    # client — we need the server running. For a pure unit test, let's
    # run the server via TestClient and set api_url accordingly.
    from fastapi.testclient import TestClient
    from robotsix_mill.runtime.api import create_app

    with TestClient(create_app(settings)) as client:
        # The CLI uses settings.api_url to reach the server.
        # We can't override that easily without monkeypatching.
        pass

    # Instead, we test the approve logic directly via the API and just
    # verify the CLI argument parsing exits correctly for the success case.
    # The full integration test requires the server to be running.
    # For this test, we'll use httpx mocks.

    import httpx

    real_client = httpx.Client

    class FakeResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
            self.text = ""

        def json(self):
            return self._json

        @property
        def is_success(self):
            return 200 <= self.status_code < 300

        def raise_for_status(self):
            if not self.is_success:
                raise httpx.HTTPStatusError("", request=None, response=self)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            if url.endswith("/approve"):
                return FakeResponse(
                    200,
                    {"id": "test-id", "state": "ready", "title": "T"},
                )
            return FakeResponse(404, {})

    # patch httpx.Client with our fake
    import robotsix_mill.cli as cli_mod

    original = cli_mod.httpx.Client
    cli_mod.httpx.Client = FakeClient
    try:
        rc = main(["ticket", "approve", "test-id"])
        assert rc == 0
    finally:
        cli_mod.httpx.Client = original


def test_approve_failure(settings, service):
    """CLI `ticket approve <id>` exits non-zero on failure (e.g. 409)."""
    import httpx
    import robotsix_mill.cli as cli_mod

    class FakeResponse:
        def __init__(self, status_code, detail):
            self.status_code = status_code
            self._detail = detail
            self.text = ""

        def json(self):
            return {"detail": self._detail}

        @property
        def is_success(self):
            return 200 <= self.status_code < 300

        def raise_for_status(self):
            if not self.is_success:
                raise httpx.HTTPStatusError("", request=None, response=self)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            return FakeResponse(
                409, "draft -> ready not allowed",
            )

    original = cli_mod.httpx.Client
    cli_mod.httpx.Client = FakeClient
    try:
        rc = main(["ticket", "approve", "bad-id"])
        assert rc == 1  # non-zero exit
    finally:
        cli_mod.httpx.Client = original


# --- resume-blocked CLI tests ---


def test_resume_blocked_success(settings, service):
    """CLI `ticket resume-blocked <id>` exits 0 on success."""
    import robotsix_mill.cli as cli_mod

    class FakeResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
            self.text = ""

        def json(self):
            return self._json

        @property
        def is_success(self):
            return 200 <= self.status_code < 300

        def raise_for_status(self):
            if not self.is_success:
                raise cli_mod.httpx.HTTPStatusError(
                    "", request=None, response=self
                )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            if url.endswith("/resume-blocked"):
                return FakeResponse(
                    200,
                    {"id": "test-id", "state": "done", "title": "T"},
                )
            return FakeResponse(404, {})

    original = cli_mod.httpx.Client
    cli_mod.httpx.Client = FakeClient
    try:
        rc = main(["ticket", "resume-blocked", "test-id"])
        assert rc == 0
    finally:
        cli_mod.httpx.Client = original


def test_resume_blocked_failure(settings, service):
    """CLI `ticket resume-blocked <id>` exits non-zero on failure (e.g. 409)."""
    import robotsix_mill.cli as cli_mod

    class FakeResponse:
        def __init__(self, status_code, detail):
            self.status_code = status_code
            self._detail = detail
            self.text = ""

        def json(self):
            return {"detail": self._detail}

        @property
        def is_success(self):
            return 200 <= self.status_code < 300

        def raise_for_status(self):
            if not self.is_success:
                raise cli_mod.httpx.HTTPStatusError(
                    "", request=None, response=self
                )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            return FakeResponse(409, "cannot resume — not BLOCKED")

    original = cli_mod.httpx.Client
    cli_mod.httpx.Client = FakeClient
    try:
        rc = main(["ticket", "resume-blocked", "bad-id"])
        assert rc == 1  # non-zero exit
    finally:
        cli_mod.httpx.Client = original
