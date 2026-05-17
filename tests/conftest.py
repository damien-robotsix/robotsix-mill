import pytest

from robotsix_mill.core import db
from robotsix_mill.config import Settings
from robotsix_mill.core.service import TicketService


@pytest.fixture
def settings(tmp_path) -> Settings:
    db.reset_engine()  # don't reuse a cached engine across tests
    s = Settings(MILL_DATA_DIR=str(tmp_path))
    db.init_db(s)
    yield s
    db.reset_engine()


@pytest.fixture
def service(settings) -> TicketService:
    return TicketService(settings)


@pytest.fixture
def fake_sandbox(monkeypatch):
    """Replace the (always-containerized) sandbox seam with a tiny
    interpreter so the suite is hermetic and never invokes Docker.
    There is no 'local' mode to fall back on by design."""
    from robotsix_mill import sandbox

    def _run(command, *, repo_dir, settings):
        c = command.strip()
        if c == "false":
            return (1, "false: command failed")
        if c.startswith("echo "):
            return (0, c[5:] + "\n")
        return (0, "")  # "true", "", and anything else: success

    monkeypatch.setattr(sandbox, "run", _run)
    return _run
