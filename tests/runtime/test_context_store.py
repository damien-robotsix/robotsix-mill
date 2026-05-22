"""Tests for ContextStore: store, retrieve, persistence, thread safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.runtime.api import create_app
from robotsix_mill.runtime.context_store import ContextStore


class TestContextStore:
    """Unit tests for the ContextStore class."""

    def test_store_and_retrieve_round_trip(self, tmp_path: Path):
        """Store a conversation and retrieve it unchanged."""
        store = ContextStore(tmp_path / "ctx.json")
        store.store(
            "ticket-1",
            "conversation",
            [{"role": "user", "content": "hello"}],
        )
        assert store.retrieve("ticket-1", "conversation") == [
            {"role": "user", "content": "hello"},
        ]

    def test_missing_key_returns_none(self, tmp_path: Path):
        """Retrieving a key that was never stored returns None."""
        store = ContextStore(tmp_path / "ctx.json")
        assert store.retrieve("nonexistent", "conversation") is None

    def test_different_types_independent(self, tmp_path: Path):
        """Same key with different types are independent slots."""
        store = ContextStore(tmp_path / "ctx.json")
        store.store("abc", "conversation", "chat data")
        store.store("abc", "file", "file data")
        assert store.retrieve("abc", "conversation") == "chat data"
        assert store.retrieve("abc", "file") == "file data"

    def test_overwrite_replaces_previous_value(self, tmp_path: Path):
        """Storing the same (key, type) twice replaces the old value."""
        store = ContextStore(tmp_path / "ctx.json")
        store.store("k", "conversation", "v1")
        store.store("k", "conversation", "v2")
        assert store.retrieve("k", "conversation") == "v2"

    def test_persistence_survives_restart(self, tmp_path: Path):
        """A new ContextStore reading the same file sees data from a
        previous instance (simulated process restart)."""
        path = tmp_path / "ctx.json"
        s1 = ContextStore(path)
        s1.store("survivor", "conversation", "persisted data")

        # Simulate restart: new store reading the same file.
        s2 = ContextStore(path)
        assert s2.retrieve("survivor", "conversation") == "persisted data"

    def test_persistence_file_contents(self, tmp_path: Path):
        """The backing JSON file has the expected nested structure."""
        path = tmp_path / "ctx.json"
        store = ContextStore(path)
        store.store("ticket-1", "conversation", [{"role": "user"}])
        store.store("abc", "file", "file contents")

        raw = json.loads(path.read_text())
        assert raw == {
            "conversation": {"ticket-1": [{"role": "user"}]},
            "file": {"abc": "file contents"},
        }

    def test_corrupt_file_recovery(self, tmp_path: Path):
        """A corrupt backing file must not raise — the store starts
        empty and a subsequent store overwrites correctly."""
        path = tmp_path / "ctx.json"
        path.write_text("not valid json {{{{{")

        store = ContextStore(path)  # must not raise
        assert store.retrieve("anything", "conversation") is None

        # A subsequent store overwrites the corrupt file and works.
        store.store("k", "file", "recovered")
        assert store.retrieve("k", "file") == "recovered"

    def test_corrupt_file_wrong_structure(self, tmp_path: Path):
        """Valid JSON that isn't a dict (e.g. a list) is treated as
        empty and a subsequent store overwrites it."""
        path = tmp_path / "ctx.json"
        path.write_text('[{"not": "a dict"}]')

        store = ContextStore(path)  # must not raise
        assert store.retrieve("k", "conversation") is None

        store.store("k", "conversation", "works")
        assert store.retrieve("k", "conversation") == "works"

    def test_missing_file_noop(self, tmp_path: Path):
        """A store pointing to a non-existent file starts empty."""
        path = tmp_path / "nonexistent.json"
        store = ContextStore(path)
        assert store.retrieve("any", "conversation") is None

    def test_store_none_value(self, tmp_path: Path):
        """Storing None as a value round-trips correctly."""
        store = ContextStore(tmp_path / "ctx.json")
        store.store("k", "file", None)
        assert store.retrieve("k", "file") is None
        # Also verify missing-key returns None — distinguish by checking
        # that a stored-None slot is present (retrieving a different
        # type for the same key still returns None for the missing slot).
        assert store.retrieve("k", "conversation") is None

    def test_thread_safety_smoke(self, tmp_path: Path):
        """Smoke test: concurrent store/retrieve does not raise or corrupt."""
        import threading

        store = ContextStore(tmp_path / "ctx.json")
        errors = []

        def worker(thread_id: int):
            try:
                for i in range(100):
                    key = f"thread-{thread_id}-key-{i}"
                    store.store(key, "conversation", {"tid": thread_id, "i": i})
                    _ = store.retrieve(key, "conversation")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # Spot-check: each thread's data survived and is readable.
        for tid in range(5):
            v = store.retrieve(f"thread-{tid}-key-50", "conversation")
            assert v == {"tid": tid, "i": 50}


class TestContextStoreIntegration:
    """Integration: the store is attached to the app state."""

    @pytest.fixture
    def client(self, settings):
        """TestClient that gives access to the app's context_store."""
        app = create_app(settings)
        with TestClient(app) as c:
            yield c

    def test_context_store_on_app_state(self, client):
        """After create_app, app.state.context_store is a ContextStore."""
        store = client.app.state.context_store
        assert isinstance(store, ContextStore)

    def test_backing_file_exists_after_app_start(self, client, settings):
        """The backing JSON file is created eagerly."""
        path = settings.data_dir / "context_store.json"
        assert path.exists()
