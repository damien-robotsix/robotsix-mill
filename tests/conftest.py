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
