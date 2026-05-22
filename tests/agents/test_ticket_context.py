import json
import threading

import pytest

from robotsix_mill.agents.ticket_context import ContextStore


class TestContextStoreDeleteConversation:
    """Unit tests for ``ContextStore.delete_conversation``."""

    def test_deletes_file_and_cache(self, tmp_path):
        store = ContextStore(tmp_path)
        store.append_messages("tkt-1", '{"role":"user","content":"hi"}')
        file_path = tmp_path / "tkt-1.json"
        assert file_path.exists()
        assert "tkt-1" in store._cache

        store.delete_conversation("tkt-1")
        assert not file_path.exists()
        assert "tkt-1" not in store._cache

    def test_idempotent(self, tmp_path):
        store = ContextStore(tmp_path)
        store.append_messages("tkt-1", '{"role":"user","content":"hi"}')
        store.delete_conversation("tkt-1")
        # Second call should be a no-op (no error).
        store.delete_conversation("tkt-1")
        assert not (tmp_path / "tkt-1.json").exists()
        assert "tkt-1" not in store._cache

    def test_missing_id_noop(self, tmp_path):
        store = ContextStore(tmp_path)
        # Never stored — should not raise.
        store.delete_conversation("nonexistent")
        assert "nonexistent" not in store._cache

    def test_get_messages_after_delete_returns_empty(self, tmp_path):
        store = ContextStore(tmp_path)
        store.append_messages("tkt-1", '{"role":"user","content":"hi"}')
        store.delete_conversation("tkt-1")
        assert store.get_messages("tkt-1") == []

    def test_concurrent_append_and_delete_no_deadlock(self, tmp_path):
        store = ContextStore(tmp_path)
        store.append_messages("tkt-1", '{"role":"user","content":"hi"}')
        errors = []

        def append_loop():
            try:
                for i in range(50):
                    store.append_messages(
                        "tkt-1", json.dumps({"role": "user", "content": f"msg{i}"})
                    )
            except Exception as e:
                errors.append(e)

        def delete_loop():
            try:
                for _ in range(50):
                    store.delete_conversation("tkt-2")
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=append_loop)
        t2 = threading.Thread(target=delete_loop)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert not errors, f"Unexpected errors: {errors}"
