"""Tests for screenshot (vision) wiring in ``run_refine_agent``.

Refine attaches user-supplied screenshots natively via
``build_agent(images=[(media_type, bytes), ...])`` with the OpenRouter
key as ``vision_api_key`` — on *every* transport. llmio delivers the
images natively to the Claude SDK and answers image questions via the
injected ``ask_image`` tool on the OpenRouter (``TierConfig.vision``)
binding. The Claude-only gate that used to restrict screenshots to the
claude_sdk path is gone, and no image bytes are embedded in the prompt.

So:

* readable screenshots → ``build_agent(images=[...], vision_api_key=...)``,
  prompt stays a plain ``str``
* no readable screenshots → no ``images=``/``vision_api_key`` kwargs,
  prompt stays a plain ``str`` (with a degraded note when screenshots
  were supplied but unreadable)
"""

from __future__ import annotations

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


class _FakeSecrets:
    openrouter_api_key = "k"


def _install_capture(monkeypatch):
    """Patch the agent seam; capture build_agent kwargs + run_sync prompt."""
    captured: dict = {}

    class _FakeHandle:
        def run_sync(self, prompt, *, message_history=None, usage_limits=None):
            captured["prompt"] = prompt
            return _FakeResult()

    def _fake_builder(*a, **k):
        captured.update(k)
        return _FakeHandle()

    monkeypatch.setattr(base, "build_agent_from_definition", _fake_builder)
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(refining, "get_secrets", lambda: _FakeSecrets())

    # run_agent simply invokes make_run on the (fake) agent.
    from robotsix_mill.agents import retry

    monkeypatch.setattr(
        retry, "run_agent", lambda agent, make_run, **k: make_run(agent)
    )
    return captured


def test_screenshot_passed_as_images_kwarg(settings, monkeypatch, tmp_path):
    """A present, readable screenshot is handed to ``build_agent`` as native
    ``images=[("image/png", bytes)]`` with the OpenRouter key as
    ``vision_api_key``. The run input stays a text-only string — no image
    bytes are embedded in the prompt."""
    captured = _install_capture(monkeypatch)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_PNG)

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[shot],
    )

    assert captured["images"] == [("image/png", _PNG)]
    assert captured["vision_api_key"] == "k"
    assert isinstance(captured["prompt"], str)


def test_no_screenshots_uses_plain_string(settings, monkeypatch):
    captured = _install_capture(monkeypatch)

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[],
    )

    assert isinstance(captured["prompt"], str)
    assert "images" not in captured
    assert "vision_api_key" not in captured


def test_multiple_screenshots_preserve_order(settings, monkeypatch, tmp_path):
    captured = _install_capture(monkeypatch)
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

    images = captured["images"]
    assert len(images) == 2
    # Input order is preserved.
    assert images[0] == ("image/png", b"AAA")
    assert images[1] == ("image/png", b"BBB")
    assert isinstance(captured["prompt"], str)


def test_media_type_mapping_per_suffix(settings, monkeypatch, tmp_path):
    for suffix, expected in (
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
    ):
        captured = _install_capture(monkeypatch)
        shot = tmp_path / f"shot{suffix}"
        shot.write_bytes(_PNG)

        refining.run_refine_agent(
            settings=settings,
            title="T",
            draft="draft text",
            screenshot_paths=[shot],
        )

        assert captured["images"][0] == (expected, _PNG)


def test_unsupported_suffix_skipped(settings, monkeypatch, tmp_path):
    captured = _install_capture(monkeypatch)
    shot = tmp_path / "notes.txt"
    shot.write_bytes(b"hello")

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[shot],
    )

    # Only path is unsupported → no images kwarg → plain str prompt + note.
    assert "images" not in captured
    assert isinstance(captured["prompt"], str)
    assert "screenshot" in captured["prompt"].lower()


def test_unreadable_file_skipped(settings, monkeypatch, tmp_path):
    captured = _install_capture(monkeypatch)
    missing = tmp_path / "gone.png"  # never created

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[missing],
    )

    # Unreadable file is skipped (no crash); only path → plain str prompt.
    assert "images" not in captured
    assert isinstance(captured["prompt"], str)


def test_all_skipped_falls_back_to_plain_string(settings, monkeypatch, tmp_path):
    captured = _install_capture(monkeypatch)
    bad_suffix = tmp_path / "notes.txt"
    bad_suffix.write_bytes(b"hi")
    missing = tmp_path / "gone.png"

    refining.run_refine_agent(
        settings=settings,
        title="T",
        draft="draft text",
        screenshot_paths=[bad_suffix, missing],
    )

    # Every screenshot skipped → no images kwarg → plain str payload + note.
    assert "images" not in captured
    assert isinstance(captured["prompt"], str)
    assert "screenshot" in captured["prompt"].lower()
