"""Tests for the survey agent periodic pass in the worker."""

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core import db


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointing to tmp_path."""
    overrides.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    # Default survey_periodic to false so the negative test is clean
    if "MILL_SURVEY_PERIODIC" not in overrides:
        overrides["MILL_SURVEY_PERIODIC"] = "false"
    s = Settings(**overrides)
    db.reset_engine()
    db.init_db(s)
    return s


@pytest.mark.asyncio
async def test_worker_survey_task_created_when_periodic(tmp_path, monkeypatch):
    """Worker._survey_task is created when MILL_SURVEY_PERIODIC=true."""
    from robotsix_mill.stages import StageContext
    from robotsix_mill.runtime.worker import Worker
    from robotsix_mill.core import db
    from robotsix_mill.core.service import TicketService

    settings = _make_settings(
        tmp_path,
        MILL_SURVEY_PERIODIC="true",
        MILL_SURVEY_INTERVAL_SECONDS="1",
    )
    db.reset_engine()
    db.init_db(settings)
    service = TicketService(settings)
    ctx = StageContext(settings=settings, service=service)

    # Patch _run_periodic_pass to be a no-op to avoid running immediately
    async def noop_periodic(self, label, runner_fn, interval):
        import asyncio
        await asyncio.sleep(3600)

    monkeypatch.setattr(Worker, "_run_periodic_pass", noop_periodic)

    worker = Worker(ctx)
    worker.start()

    assert worker._survey_task is not None
    assert not worker._survey_task.done()

    await worker.stop()


@pytest.mark.asyncio
async def test_worker_survey_task_not_created_when_periodic_false(tmp_path, monkeypatch):
    """Worker._survey_task is NOT created when MILL_SURVEY_PERIODIC=false."""
    from robotsix_mill.stages import StageContext
    from robotsix_mill.runtime.worker import Worker
    from robotsix_mill.core import db
    from robotsix_mill.core.service import TicketService

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)
    service = TicketService(settings)
    ctx = StageContext(settings=settings, service=service)

    worker = Worker(ctx)
    worker.start()

    assert worker._survey_task is None

    await worker.stop()
