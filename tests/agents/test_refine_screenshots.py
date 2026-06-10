"""Tests for screenshot (vision) wiring in ``run_refine_agent``.

Verifies the prompt payload handed to ``run_sync`` depends on the
backend gate:

* claude_sdk path + screenshots → list ``[str, BinaryContent, ...]``
* DeepSeek default path → plain ``str`` (no ``BinaryContent``).
"""

from __future__ import annotations

from pydantic_ai import BinaryContent

from robotsix_mill.agents import base, refining
from robotsix_mill.agents.refining import RefineResult


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


class _FakeResult:
    def __init__(self) -> None:
        self.output = RefineResult(split=False, spec_markdown="## Problem\nspec\n")
        self.response = type("R", (), {"finish_reason": None})()

    def all_messages_json(self) -> bytes:
        return b"[]"

    def new_messages_json(self) -> bytes:
        return b"[]"


def _install_capture(monkeypatch, *, claude_sdk: bool):
    """Patch the agent seam; return a dict capturing the run_sync prompt."""
    captured: dict = {}

    class _FakeHandle:
        def run_sync(self, prompt, *, message_history=None, usage_limits=None):
            captured["prompt"] = prompt
            return _FakeResult()

    monkeypatch.setattr(
        base, "build_agent_from_definition", lambda *a, **k: _FakeHandle()
    )
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(base, "_use_claude_sdk", lambda settings, name: claude_sdk)

    # run_agent simply invokes make_run on the (fake) agent.
    from robotsix_mill.agents import retry

    monkeypatch.setattr(
        retry, "run_agent", lambda agent, make_run, **k: make_run(agent)
    )
    return captured


def test_claude_sdk_path_attaches_binary_content(settings, monkeypatch, tmp_path):
    captured = _install_capture(monkeypatch, claude_sdk=True)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_PNG)

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[shot],
    )

    prompt = captured["prompt"]
    assert isinstance(prompt, list)
    assert isinstance(prompt[0], str)
    images = prompt[1:]
    assert len(images) == 1
    assert isinstance(images[0], BinaryContent)
    assert images[0].media_type == "image/png"
    assert images[0].data == _PNG


def test_deepseek_path_uses_plain_string(settings, monkeypatch, tmp_path):
    captured = _install_capture(monkeypatch, claude_sdk=False)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_PNG)

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[shot],
    )

    prompt = captured["prompt"]
    assert isinstance(prompt, str)
    # The agent is told screenshots exist even though it can't see them.
    assert "screenshot" in prompt.lower()


def test_no_screenshots_uses_plain_string(settings, monkeypatch):
    captured = _install_capture(monkeypatch, claude_sdk=True)

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[],
    )

    assert isinstance(captured["prompt"], str)


def test_multiple_screenshots_preserve_order(settings, monkeypatch, tmp_path):
    captured = _install_capture(monkeypatch, claude_sdk=True)
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[a, b],
    )

    prompt = captured["prompt"]
    assert isinstance(prompt, list)
    assert isinstance(prompt[0], str)
    images = prompt[1:]
    assert len(images) == 2
    assert all(isinstance(i, BinaryContent) for i in images)
    # Input order is preserved.
    assert images[0].data == b"AAA"
    assert images[1].data == b"BBB"


def test_media_type_mapping_per_suffix(settings, monkeypatch, tmp_path):
    for suffix, expected in (
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
    ):
        captured = _install_capture(monkeypatch, claude_sdk=True)
        shot = tmp_path / f"shot{suffix}"
        shot.write_bytes(_PNG)

        refining.run_refine_agent(
            settings=settings,
            title="T",
            draft="draft text",
            screenshot_paths=[shot],
        )

        prompt = captured["prompt"]
        assert isinstance(prompt, list)
        assert prompt[1].media_type == expected


def test_unsupported_suffix_skipped(settings, monkeypatch, tmp_path):
    captured = _install_capture(monkeypatch, claude_sdk=True)
    shot = tmp_path / "notes.txt"
    shot.write_bytes(b"hello")

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[shot],
    )

    # Only path is unsupported → no BinaryContent → plain str prompt.
    assert isinstance(captured["prompt"], str)


def test_unreadable_file_skipped(settings, monkeypatch, tmp_path):
    captured = _install_capture(monkeypatch, claude_sdk=True)
    missing = tmp_path / "gone.png"  # never created

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[missing],
    )

    # Unreadable file is skipped (no crash); only path → plain str prompt.
    assert isinstance(captured["prompt"], str)


def test_all_skipped_falls_back_to_plain_string(settings, monkeypatch, tmp_path):
    captured = _install_capture(monkeypatch, claude_sdk=True)
    bad_suffix = tmp_path / "notes.txt"
    bad_suffix.write_bytes(b"hi")
    missing = tmp_path / "gone.png"

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[bad_suffix, missing],
    )

    # Every screenshot skipped → _vision collapses → plain str payload.
    assert isinstance(captured["prompt"], str)
