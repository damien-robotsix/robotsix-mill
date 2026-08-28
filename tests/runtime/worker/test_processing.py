"""Unit tests for runtime/worker/processing.py helper functions."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker.processing import (
    _TERMINAL,
    _block_ticket_and_notify,
    _file_infra_ticket,
    _handle_stage_error,
    _maybe_reevaluate_epic,
    _post_trace_event,
    _root_input_summary,
    _root_output_summary,
    _root_span_attributes,
    _StageDeadlineExceeded,
)
from robotsix_mill.stages import Outcome, StageContext

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx(settings, service, repo_config):
    """Reuse the same StageContext shape as test_core.py."""
    return StageContext(settings=settings, service=service, repo_config=repo_config)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_ticket(**overrides):
    """Build a SimpleNamespace with realistic Ticket-like defaults."""
    defaults = {
        "id": "ticket-1",
        "state": State.DRAFT,
        "kind": TicketKind.TASK,
        "retry_attempt": 0,
        "parent_id": None,
        "title": "test ticket",
        "source": "test",
        "priority": False,
        "blocked_from": None,
        "paused_from": None,
        "review_rounds": 0,
        "implement_cycles": 0,
        "next_retry_at": None,
        "last_transient_error": None,
        "workspace_path": None,
        "board_id": "test-board",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ===================================================================
# _post_trace_event
# ===================================================================


class TestPostTraceEvent:
    def test_trace_id_none_noop(self, ctx):
        """trace_id=None → no-op; add_history_note never called."""
        ctx.service.add_history_note = MagicMock()
        _post_trace_event(ctx, "t-1", None, "refine")
        ctx.service.add_history_note.assert_not_called()

    def test_langfuse_url_none_noop(self, ctx, monkeypatch):
        """langfuse_trace_url returns None (Langfuse unconfigured) → no-op."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.langfuse_trace_url",
            lambda trace_id, repo_config=None: None,
        )
        ctx.service.add_history_note = MagicMock()
        _post_trace_event(ctx, "t-1", "trace-abc", "refine")
        ctx.service.add_history_note.assert_not_called()

    def test_normal_path(self, ctx, monkeypatch):
        """Posts a history note containing the trace URL and stage name."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.langfuse_trace_url",
            lambda trace_id, repo_config=None: "https://lf.example/trace/trace-abc",
        )
        ctx.service.add_history_note = MagicMock()
        _post_trace_event(ctx, "t-1", "trace-abc", "implement")
        ctx.service.add_history_note.assert_called_once()
        args = ctx.service.add_history_note.call_args
        assert args[0][0] == "t-1"
        note = args[0][1]
        assert "https://lf.example/trace/trace-abc" in note
        assert "implement" in note

    def test_add_history_note_raises_no_propagation(self, ctx, monkeypatch):
        """When add_history_note raises, the exception is caught + logged."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.langfuse_trace_url",
            lambda trace_id, repo_config=None: "https://lf.example/trace/trace-abc",
        )
        ctx.service.add_history_note = MagicMock(side_effect=RuntimeError("boom"))
        # Must not propagate
        _post_trace_event(ctx, "t-1", "trace-abc", "refine")
        ctx.service.add_history_note.assert_called_once()


# ===================================================================
# _block_ticket_and_notify
# ===================================================================


class TestBlockTicketAndNotify:
    @pytest.mark.asyncio
    async def test_full_path(self, ctx, monkeypatch):
        """Calls post_trace_event, transition, get, and send_notification."""
        post_trace = MagicMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._post_trace_event",
            post_trace,
        )
        ctx.service.transition = MagicMock()
        t = _fake_ticket(id="t-1", state=State.BLOCKED)
        ctx.service.get = MagicMock(return_value=t)
        notify = MagicMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.send_notification",
            notify,
        )

        await _block_ticket_and_notify("t-1", ctx, "refine", "boom!", "tr-1")

        post_trace.assert_called_once_with(ctx, "t-1", "tr-1", "refine")
        ctx.service.transition.assert_called_once_with(
            "t-1", State.BLOCKED, note="boom!"
        )
        ctx.service.get.assert_called_once_with("t-1")
        notify.assert_called_once_with(t, State.BLOCKED, "boom!", ctx.settings)

    @pytest.mark.asyncio
    async def test_get_returns_none_skips_notification(self, ctx, monkeypatch):
        """When ctx.service.get returns None, notification is skipped."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._post_trace_event",
            MagicMock(),
        )
        ctx.service.transition = MagicMock()
        ctx.service.get = MagicMock(return_value=None)
        notify = MagicMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.send_notification",
            notify,
        )

        await _block_ticket_and_notify("t-1", ctx, "refine", "boom!", "tr-1")

        notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_note_truncated_to_200_chars(self, ctx, monkeypatch):
        """Notes longer than 200 chars are sliced before transition/notify."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._post_trace_event",
            MagicMock(),
        )
        ctx.service.transition = MagicMock()
        t = _fake_ticket(id="t-1")
        ctx.service.get = MagicMock(return_value=t)
        notify = MagicMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.send_notification",
            notify,
        )

        long_note = "x" * 300
        await _block_ticket_and_notify("t-1", ctx, "refine", long_note, "tr-1")

        truncated = "x" * 200
        ctx.service.transition.assert_called_once_with(
            "t-1", State.BLOCKED, note=truncated
        )
        notify.assert_called_once_with(t, State.BLOCKED, truncated, ctx.settings)


# ===================================================================
# _handle_stage_error
# ===================================================================


class TestHandleStageError:
    """Tests for the big error-classification + branching function."""

    # -- helpers to reduce boilerplate in each test -------------------

    @staticmethod
    def _patch_classify(monkeypatch, classification="transient"):
        monkeypatch.setattr(
            "robotsix_mill.runtime.transient_errors.classify_stage_error",
            lambda e: classification,
        )

    @staticmethod
    def _patch_disk(monkeypatch, is_full=False):
        # is_disk_full_error is imported locally inside _handle_stage_error
        # via ``from ..transient_errors import ...`` — patch at the source
        # module, like _patch_model_outage does.
        monkeypatch.setattr(
            "robotsix_mill.runtime.transient_errors.is_disk_full_error",
            lambda e: is_full,
        )
        # first_full_path is imported at module level in processing.py.
        if is_full:
            monkeypatch.setattr(
                "robotsix_mill.runtime.worker.processing.first_full_path",
                lambda paths, min_free_mb: paths[0] if paths else None,
            )
        else:
            monkeypatch.setattr(
                "robotsix_mill.runtime.worker.processing.first_full_path",
                lambda paths, min_free_mb: None,
            )

    @staticmethod
    def _patch_network(monkeypatch, is_down=False, available=True):
        monkeypatch.setattr(
            "robotsix_mill.runtime.transient_errors.is_network_down_error",
            lambda e: is_down,
        )
        monkeypatch.setattr(
            "robotsix_mill.runtime.transient_errors.network_available",
            lambda host, cache_seconds=30.0: available,
        )

    @staticmethod
    def _patch_retry(monkeypatch, delay=5.0):
        monkeypatch.setattr(
            "robotsix_mill.runtime.stage_retry.compute_retry_delay",
            lambda attempt, base, cap: delay,
        )

    @staticmethod
    def _patch_tracing(monkeypatch):
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.tracing.set_current_span_attribute",
            lambda key, value: None,
        )

    @staticmethod
    def _patch_post_trace(monkeypatch):
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._post_trace_event",
            MagicMock(),
        )

    @staticmethod
    def _patch_block_and_notify(monkeypatch):
        mock = AsyncMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._block_ticket_and_notify",
            mock,
        )
        return mock

    @staticmethod
    def _patch_reap(monkeypatch, return_value=0):
        mock = MagicMock(return_value=return_value)
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.reap_orphan_sandboxes",
            mock,
        )
        return mock

    @staticmethod
    def _patch_model_outage(monkeypatch, is_outage=False):
        monkeypatch.setattr(
            "robotsix_mill.runtime.transient_errors.is_model_unavailable_error",
            lambda e: is_outage,
        )

    # -- tests -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_timeout_error_reaps_orphan_sandboxes(self, ctx, monkeypatch):
        """TimeoutError triggers reap_orphan_sandboxes call."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        reap = self._patch_reap(monkeypatch, return_value=3)

        t = _fake_ticket(retry_attempt=0)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        await _handle_stage_error(
            "ticket-1", ctx, "refine", TimeoutError("timed out"), "tr-1"
        )

        reap.assert_called_once()
        # timeout → sets error.subtype span attribute; reaped count also stamped

    @pytest.mark.asyncio
    async def test_stage_error_records_the_exception_for_langfuse(
        self, ctx, monkeypatch
    ):
        """The raised error reaches tracing.record_exception.

        Span *attributes* alone left every failed stage rendering as
        "[ERROR] None" in Langfuse trace summaries — the message comes from
        the recorded exception.
        """
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_retry(monkeypatch, delay=10.0)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        recorded: list = []
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.tracing.record_exception",
            recorded.append,
        )

        t = _fake_ticket(retry_attempt=1)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        err = RuntimeError("transient glitch")
        await _handle_stage_error("ticket-1", ctx, "refine", err, "tr-1")

        assert recorded == [err]

    @pytest.mark.asyncio
    async def test_transient_retry_remaining_increments_attempt(self, ctx, monkeypatch):
        """Transient with retry budget left → set_retry_state with attempt+1."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_retry(monkeypatch, delay=10.0)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        t = _fake_ticket(retry_attempt=1)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        await _handle_stage_error(
            "ticket-1", ctx, "refine", RuntimeError("transient glitch"), "tr-1"
        )

        ctx.service.set_retry_state.assert_called_once()
        kwargs = ctx.service.set_retry_state.call_args[1]
        assert kwargs["retry_attempt"] == 2  # incremented
        assert "transient glitch" in kwargs["last_transient_error"]
        assert kwargs["next_retry_at"] is not None

    @pytest.mark.asyncio
    async def test_transient_retries_exhausted_blocks(self, ctx, monkeypatch):
        """Transient but retry budget exhausted → _block_ticket_and_notify."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_reap(monkeypatch)
        block_mock = self._patch_block_and_notify(monkeypatch)

        max_attempts = ctx.settings.stage_retry_max_attempts
        t = _fake_ticket(retry_attempt=max_attempts)  # exhausted
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        await _handle_stage_error(
            "ticket-1", ctx, "refine", RuntimeError("persistent"), "tr-1"
        )

        ctx.service.set_retry_state.assert_not_called()
        block_mock.assert_called_once()
        call_args = block_mock.call_args
        assert call_args[0][0] == "ticket-1"
        assert "persisted" in call_args[0][3]  # note

    @pytest.mark.asyncio
    async def test_fatal_error_blocks_immediately(self, ctx, monkeypatch):
        """Fatal classification → _block_ticket_and_notify without retry."""
        self._patch_classify(monkeypatch, "fatal")
        self._patch_network(monkeypatch)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_reap(monkeypatch)
        block_mock = self._patch_block_and_notify(monkeypatch)

        t = _fake_ticket(retry_attempt=0)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        await _handle_stage_error(
            "ticket-1", ctx, "refine", ValueError("fatal error"), "tr-1"
        )

        ctx.service.set_retry_state.assert_not_called()
        block_mock.assert_called_once()
        call_args = block_mock.call_args
        assert "Fatal:" in call_args[0][3]

    @pytest.mark.asyncio
    async def test_network_outage_parks_without_consuming_retry(self, ctx, monkeypatch):
        """Network outage → parked (retry_attempt NOT incremented)."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch, is_down=True, available=False)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        t = _fake_ticket(retry_attempt=0)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        await _handle_stage_error(
            "ticket-1", ctx, "refine", OSError("network unreachable"), "tr-1"
        )

        ctx.service.set_retry_state.assert_called_once()
        kwargs = ctx.service.set_retry_state.call_args[1]
        # Floor at 1, not incremented — budget untouched.
        assert kwargs["retry_attempt"] == 1
        assert "network outage" in kwargs["last_transient_error"]
        assert kwargs["next_retry_at"] is not None

    @pytest.mark.asyncio
    async def test_network_outage_preserves_existing_retry_attempt(
        self, ctx, monkeypatch
    ):
        """Network outage with retry_attempt=3 → floors at current, not 1."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch, is_down=True, available=False)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        t = _fake_ticket(retry_attempt=3)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        await _handle_stage_error(
            "ticket-1", ctx, "refine", OSError("unreachable"), "tr-1"
        )

        kwargs = ctx.service.set_retry_state.call_args[1]
        assert kwargs["retry_attempt"] == 3  # preserved, not reset

    @pytest.mark.asyncio
    async def test_disk_full_parks_and_files_infra_ticket(self, ctx, monkeypatch):
        """Disk-full error parks the ticket and files an infra ticket."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_model_outage(monkeypatch, is_outage=False)
        self._patch_disk(monkeypatch, is_full=True)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        t = _fake_ticket(retry_attempt=0)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        infra_mock = MagicMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._file_infra_ticket",
            infra_mock,
        )

        await _handle_stage_error(
            "ticket-1", ctx, "implement", OSError("No space left on device"), "tr-1"
        )

        # Must park (not consume retry).
        ctx.service.set_retry_state.assert_called_once()
        kwargs = ctx.service.set_retry_state.call_args[1]
        assert kwargs["retry_attempt"] == 1  # max(ticket.retry_attempt, 1)
        assert "data volume full" in kwargs["last_transient_error"]
        # Must file infra ticket.
        infra_mock.assert_called_once()
        call_args = infra_mock.call_args
        assert call_args[0][0] is ctx
        assert "disk space low" in call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_model_outage_parks_without_consuming_retry(self, ctx, monkeypatch):
        """Model outage → parked (park count tracked via retry_attempt)."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_model_outage(monkeypatch, is_outage=True)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        t = _fake_ticket(retry_attempt=0)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        await _handle_stage_error(
            "ticket-1", ctx, "refine", RuntimeError("model unavailable"), "tr-1"
        )

        ctx.service.set_retry_state.assert_called_once()
        kwargs = ctx.service.set_retry_state.call_args[1]
        # Park count = 1 (retry_attempt 0 → 1)
        assert kwargs["retry_attempt"] == 1
        assert "model outage" in kwargs["last_transient_error"]
        assert "model_outage" in kwargs["last_transient_error"]
        assert kwargs["next_retry_at"] is not None

    @pytest.mark.asyncio
    async def test_model_outage_increments_park_count(self, ctx, monkeypatch):
        """Model outage with existing retry_attempt → park count increments."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_model_outage(monkeypatch, is_outage=True)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        t = _fake_ticket(retry_attempt=3)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        await _handle_stage_error(
            "ticket-1", ctx, "refine", RuntimeError("model unavailable"), "tr-1"
        )

        kwargs = ctx.service.set_retry_state.call_args[1]
        # Park count increments: 3 → 4
        assert kwargs["retry_attempt"] == 4
        assert "model outage" in kwargs["last_transient_error"]

    @pytest.mark.asyncio
    async def test_model_outage_exceeds_max_parks_blocks(self, ctx, monkeypatch):
        """When park count exceeds model_outage_max_parks → BLOCKED."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_model_outage(monkeypatch, is_outage=True)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        block_mock = self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        # model_outage_max_parks default is 20, so retry_attempt=20
        # means the next park would be 21 > 20 → blocks.
        t = _fake_ticket(retry_attempt=20)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        await _handle_stage_error(
            "ticket-1", ctx, "refine", RuntimeError("model unavailable"), "tr-1"
        )

        # Must NOT park — must block.
        ctx.service.set_retry_state.assert_not_called()
        block_mock.assert_called_once()
        call_args = block_mock.call_args
        assert "Infrastructure:" in call_args[0][3]
        assert "model_outage" in call_args[0][3]

    @pytest.mark.asyncio
    async def test_model_outage_not_triggered_on_generic_503(self, ctx, monkeypatch):
        """A generic transient error (not model outage) → normal retry path."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_model_outage(monkeypatch, is_outage=False)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        t = _fake_ticket(retry_attempt=0)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        await _handle_stage_error(
            "ticket-1", ctx, "refine", RuntimeError("generic transient"), "tr-1"
        )

        kwargs = ctx.service.set_retry_state.call_args[1]
        # Normal retry: attempt increments to 1, "model outage" absent.
        assert kwargs["retry_attempt"] == 1
        assert "model outage" not in kwargs["last_transient_error"]

    @pytest.mark.asyncio
    async def test_implement_transient_clears_fingerprint_guard(self, ctx, monkeypatch):
        """Implement stage transient → clears artifacts/implement.md."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        t = _fake_ticket(retry_attempt=0)
        ctx.service.get = MagicMock(return_value=t)
        ctx.service.set_retry_state = MagicMock()

        # Mock workspace to return a fake artifacts_dir
        fake_artifacts_dir = MagicMock()
        # Make the / operator return a MagicMock that supports unlink
        fake_implement_md = MagicMock()
        fake_artifacts_dir.__truediv__ = MagicMock(return_value=fake_implement_md)
        fake_workspace = MagicMock()
        fake_workspace.artifacts_dir = fake_artifacts_dir
        ctx.service.workspace = MagicMock(return_value=fake_workspace)

        await _handle_stage_error(
            "ticket-1", ctx, "implement", RuntimeError("infra hiccup"), "tr-1"
        )

        ctx.service.workspace.assert_called_once_with(t)
        fake_implement_md.unlink.assert_called_once_with(missing_ok=True)

    @pytest.mark.asyncio
    async def test_implement_transient_ticket_gone_skips_guard(self, ctx, monkeypatch):
        """If ticket.get returns None inside the implement guard, skip it."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        ctx.service.get = MagicMock(return_value=None)  # vanished
        ctx.service.set_retry_state = MagicMock()
        ctx.service.workspace = MagicMock()

        # Should not raise
        await _handle_stage_error(
            "ticket-1", ctx, "implement", RuntimeError("glitch"), "tr-1"
        )

        ctx.service.workspace.assert_not_called()
        ctx.service.set_retry_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_ticket_vanished_bails_early(self, ctx, monkeypatch):
        """If ticket.get returns None (not just inside guard), bail early."""
        self._patch_classify(monkeypatch, "transient")
        self._patch_network(monkeypatch)
        self._patch_retry(monkeypatch)
        self._patch_tracing(monkeypatch)
        self._patch_post_trace(monkeypatch)
        self._patch_block_and_notify(monkeypatch)
        self._patch_reap(monkeypatch)

        ctx.service.get = MagicMock(return_value=None)
        ctx.service.set_retry_state = MagicMock()

        await _handle_stage_error(
            "ticket-1", ctx, "refine", RuntimeError("glitch"), "tr-1"
        )

        ctx.service.set_retry_state.assert_not_called()


# ===================================================================
# _maybe_reevaluate_epic
# ===================================================================


class TestFileInfraTicket:
    """Tests for _file_infra_ticket — auto-files infrastructure blocker tickets."""

    @pytest.fixture(autouse=True)
    def _reset_cooldown(self):
        """Clear the cooldown dict between tests."""
        from robotsix_mill.runtime.worker.processing import _INFRA_TICKET_LAST

        _INFRA_TICKET_LAST.clear()
        yield
        _INFRA_TICKET_LAST.clear()

    def test_files_ticket_on_first_call(self, ctx, monkeypatch):
        """First call files a ticket with source=infrastructure and priority."""
        mock_service = MagicMock()
        mock_service.list.return_value = []
        mock_service.create.return_value = MagicMock(id="t-infra-1")
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.TicketService",
            lambda settings, board_id="": mock_service,
        )
        ctx.service.board_id = "test-board"

        _file_infra_ticket(ctx, "Disk space low", "disk is full")

        mock_service.create.assert_called_once()
        call_kwargs = mock_service.create.call_args
        assert call_kwargs[0][0] == "Disk space low"
        assert "Automatically filed" in call_kwargs[0][1]
        assert call_kwargs[1]["source"] == "infrastructure"
        assert call_kwargs[1]["priority"] is True

    def test_cooldown_suppresses_repeat(self, ctx, monkeypatch):
        """Second call within cooldown window is suppressed."""
        mock_service = MagicMock()
        mock_service.list.return_value = []
        mock_service.create.return_value = MagicMock(id="t-infra-1")
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.TicketService",
            lambda settings, board_id="": mock_service,
        )
        ctx.service.board_id = "test-board"

        _file_infra_ticket(ctx, "Disk space low", "disk is full")
        _file_infra_ticket(ctx, "Disk space low", "disk is full")

        assert mock_service.create.call_count == 1

    def test_dedup_skips_existing_open_ticket(self, ctx, monkeypatch):
        """When a non-terminal ticket with same title exists, skip."""
        from robotsix_mill.core.states import State

        existing = MagicMock()
        existing.title = "Disk space low"
        existing.state = State.DRAFT
        mock_service = MagicMock()
        mock_service.list.return_value = [existing]
        mock_service.create.return_value = MagicMock(id="t-infra-1")
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.TicketService",
            lambda settings, board_id="": mock_service,
        )
        ctx.service.board_id = "test-board"

        _file_infra_ticket(ctx, "Disk space low", "disk is full")

        mock_service.create.assert_not_called()

    def test_existing_closed_ticket_does_not_block(self, ctx, monkeypatch):
        """A CLOSED ticket with same title does not suppress filing."""
        from robotsix_mill.core.states import State

        existing = MagicMock()
        existing.title = "Disk space low"
        existing.state = State.CLOSED
        mock_service = MagicMock()
        mock_service.list.return_value = [existing]
        mock_service.create.return_value = MagicMock(id="t-infra-2")
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.TicketService",
            lambda settings, board_id="": mock_service,
        )
        ctx.service.board_id = "test-board"

        _file_infra_ticket(ctx, "Disk space low", "disk is full")

        mock_service.create.assert_called_once()

    def test_exception_does_not_propagate(self, ctx, monkeypatch):
        """Service creation failure is caught and logged, not raised."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing.TicketService",
            lambda settings, board_id="": (_ for _ in ()).throw(
                RuntimeError("db down")
            ),
        )
        ctx.service.board_id = "test-board"

        # Must not raise.
        _file_infra_ticket(ctx, "Disk space low", "disk is full")


class TestMaybeReevaluateEpic:
    def test_terminal_state_triggers_reeval(self, ctx, monkeypatch):
        """next_state in _EPIC_CHILD_TERMINAL + EPIC parent → spawns reeval."""
        spawn = MagicMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._spawn_epic_reeval",
            spawn,
        )
        child = _fake_ticket(id="child-1", state=State.DONE, parent_id="epic-1")
        parent = _fake_ticket(id="epic-1", kind=TicketKind.EPIC)
        ctx.service.get = MagicMock(side_effect=[child, parent])

        _maybe_reevaluate_epic("child-1", ctx, State.DONE)

        spawn.assert_called_once_with("epic-1", ctx)

    def test_non_terminal_state_noop(self, ctx, monkeypatch):
        """next_state NOT in terminal set → no-op."""
        spawn = MagicMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._spawn_epic_reeval",
            spawn,
        )
        ctx.service.get = MagicMock()

        _maybe_reevaluate_epic("child-1", ctx, State.READY)

        spawn.assert_not_called()
        ctx.service.get.assert_not_called()

    def test_no_parent_id_noop(self, ctx, monkeypatch):
        """Ticket has no parent_id → no-op."""
        spawn = MagicMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._spawn_epic_reeval",
            spawn,
        )
        child = _fake_ticket(id="child-1", state=State.CLOSED, parent_id=None)
        ctx.service.get = MagicMock(return_value=child)

        _maybe_reevaluate_epic("child-1", ctx, State.CLOSED)

        ctx.service.get.assert_called_once_with("child-1")
        spawn.assert_not_called()

    def test_parent_not_epic_noop(self, ctx, monkeypatch):
        """Parent exists but is not EPIC → no-op."""
        spawn = MagicMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._spawn_epic_reeval",
            spawn,
        )
        child = _fake_ticket(id="child-1", state=State.DONE, parent_id="parent-1")
        parent = _fake_ticket(id="parent-1", kind=TicketKind.TASK)
        ctx.service.get = MagicMock(side_effect=[child, parent])

        _maybe_reevaluate_epic("child-1", ctx, State.DONE)

        spawn.assert_not_called()

    def test_ticket_not_found_noop(self, ctx, monkeypatch):
        """Ticket not found → no-op (no crash)."""
        spawn = MagicMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._spawn_epic_reeval",
            spawn,
        )
        ctx.service.get = MagicMock(return_value=None)

        _maybe_reevaluate_epic("child-1", ctx, State.CLOSED)

        spawn.assert_not_called()

    def test_parent_not_found_noop(self, ctx, monkeypatch):
        """Child found but parent not found → no-op."""
        spawn = MagicMock()
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.processing._spawn_epic_reeval",
            spawn,
        )
        child = _fake_ticket(id="child-1", state=State.ANSWERED, parent_id="ghost")
        ctx.service.get = MagicMock(side_effect=[child, None])

        _maybe_reevaluate_epic("child-1", ctx, State.ANSWERED)

        spawn.assert_not_called()


# ===================================================================
# _root_span_attributes
# ===================================================================


class TestRootSpanAttributes:
    def test_full_ticket_all_attributes(self):
        """Ticket with all fields populated → every key present."""
        t = SimpleNamespace(
            state=State.READY,
            kind=TicketKind.TASK,
            retry_attempt=2,
            review_rounds=1,
            implement_cycles=3,
            blocked_from="refine",
            paused_from=None,
            source="audit",
        )
        counts = Counter({"implement": 4, "refine": 1})

        attrs = _root_span_attributes(t, "implement", counts)

        assert attrs["ticket.state"] == State.READY.value
        assert attrs["ticket.kind"] == TicketKind.TASK.value
        assert attrs["ticket.retry_attempt"] == "2"
        assert attrs["ticket.review_rounds"] == "1"
        assert attrs["ticket.implement_cycles"] == "3"
        assert attrs["ticket.blocked_from"] == "refine"
        assert attrs["ticket.paused_from"] == ""  # None → ""
        assert attrs["ticket.dispatch_count"] == "4"
        assert attrs["ticket.source"] == "audit"
        assert attrs["stage.name"] == "implement"

    def test_none_blocked_paused_default_to_empty(self):
        """blocked_from=None, paused_from=None → empty strings."""
        t = SimpleNamespace(
            state=State.DRAFT,
            kind=TicketKind.INQUIRY,
            retry_attempt=0,
            review_rounds=0,
            implement_cycles=0,
            blocked_from=None,
            paused_from=None,
            source="",
        )
        attrs = _root_span_attributes(t, "refine", Counter())
        assert attrs["ticket.blocked_from"] == ""
        assert attrs["ticket.paused_from"] == ""

    def test_no_kind_attribute_returns_empty_string(self):
        """Ticket without 'kind' attribute → kind key is ''."""
        t = SimpleNamespace(
            state=State.READY,
            retry_attempt=0,
            review_rounds=0,
            implement_cycles=0,
            blocked_from=None,
            paused_from=None,
            source="",
        )
        # No 'kind' attribute at all
        attrs = _root_span_attributes(t, "review", Counter())
        assert attrs["ticket.kind"] == ""

    def test_dispatch_count_for_missing_stage(self):
        """dispatch_counts.get returns 0 for unseen stage."""
        t = SimpleNamespace(
            state=State.DONE,
            kind=TicketKind.TASK,
            retry_attempt=0,
            review_rounds=0,
            implement_cycles=0,
            blocked_from=None,
            paused_from=None,
            source="",
        )
        counts = Counter({"refine": 3})
        attrs = _root_span_attributes(t, "review", counts)
        assert attrs["ticket.dispatch_count"] == "0"


# ===================================================================
# _root_input_summary
# ===================================================================


class TestRootInputSummary:
    def test_full_ticket_all_keys(self):
        """All ticket fields → every key present."""
        t = SimpleNamespace(
            title="my ticket",
            state=State.READY,
            kind=TicketKind.TASK,
            source="audit",
            priority=True,
            retry_attempt=1,
            last_transient_error="timeout",
            review_rounds=2,
            implement_cycles=3,
            blocked_from="refine",
            paused_from=None,
            workspace_path="/tmp/ws",
        )
        summary = _root_input_summary(t, "t-1", "implement", dispatch_count=5)

        assert summary["ticket_id"] == "t-1"
        assert summary["title"] == "my ticket"
        assert summary["state"] == State.READY.value
        assert summary["kind"] == TicketKind.TASK.value
        assert summary["stage"] == "implement"
        assert summary["source"] == "audit"
        assert summary["priority"] is True
        assert summary["retry_attempt"] == 1
        assert summary["last_transient_error"] == "timeout"
        assert summary["review_rounds"] == 2
        assert summary["implement_cycles"] == 3
        assert summary["blocked_from"] == "refine"
        assert summary["paused_from"] is None
        assert summary["dispatch_count"] == 5
        assert summary["workspace_path"] == "/tmp/ws"

    def test_missing_optional_fields_default(self):
        """Ticket without optional getattr fields → defaults."""
        t = SimpleNamespace(
            title="minimal",
            state=State.DRAFT,
            source="",
        )
        # No kind, priority, retry_attempt, etc.
        summary = _root_input_summary(t, "t-2", "refine", dispatch_count=0)

        assert summary["ticket_id"] == "t-2"
        assert summary["priority"] is False  # getattr default
        assert summary["retry_attempt"] == 0
        assert summary["last_transient_error"] is None
        assert summary["review_rounds"] == 0
        assert summary["implement_cycles"] == 0
        assert summary["blocked_from"] is None
        assert summary["paused_from"] is None
        assert summary["workspace_path"] is None
        assert summary["dispatch_count"] == 0


# ===================================================================
# _root_output_summary
# ===================================================================


class TestRootOutputSummary:
    def test_normal_outcome(self):
        """Outcome with next_state different from ticket.state."""
        t = _fake_ticket(state=State.READY)
        outcome = Outcome(State.DELIVERABLE, "shipped!")
        summary = _root_output_summary(outcome, t)

        assert summary["next_state"] == State.DELIVERABLE.value
        assert summary["note"] == "shipped!"
        assert summary["no_op"] is False

    def test_none_outcome(self):
        """None outcome → None next_state, empty note."""
        t = _fake_ticket(state=State.DRAFT)
        summary = _root_output_summary(None, t)

        assert summary["next_state"] is None
        assert summary["note"] == ""
        assert summary["no_op"] is False

    def test_noop_outcome(self):
        """Same-state outcome → no_op=True."""
        t = _fake_ticket(state=State.READY)
        outcome = Outcome(State.READY, "still waiting")
        summary = _root_output_summary(outcome, t)

        assert summary["next_state"] == State.READY.value
        assert summary["note"] == "still waiting"
        assert summary["no_op"] is True

    def test_outcome_none_note(self):
        """Outcome with note=None → empty string."""
        t = _fake_ticket(state=State.CODE_REVIEW)
        outcome = Outcome(State.READY, None)
        summary = _root_output_summary(outcome, t)

        assert summary["note"] == ""
        assert summary["no_op"] is False


# ===================================================================
# _StageDeadlineExceeded
# ===================================================================


class TestStageDeadlineExceeded:
    def test_is_exception(self):
        """Sanity: _StageDeadlineExceeded is an Exception subclass."""
        assert issubclass(_StageDeadlineExceeded, Exception)

    def test_can_be_caught(self):
        """It can be raised and caught."""
        with pytest.raises(_StageDeadlineExceeded):
            raise _StageDeadlineExceeded("deadline")


# ===================================================================
# _TERMINAL
# ===================================================================


class TestTerminal:
    def test_terminal_states(self):
        """_TERMINAL contains CLOSED, ERRORED, BLOCKED."""
        assert _TERMINAL == {State.CLOSED, State.ERRORED, State.BLOCKED}

    def test_done_is_not_terminal(self):
        """Regression: DONE must NOT be in _TERMINAL — retrospect owns it."""
        assert State.DONE not in _TERMINAL
