import asyncio
import subprocess

import pytest

from robotsix_mill import sandbox
from robotsix_mill.agents import web_research as wr
from robotsix_mill.agents.base import compose_prompt
from robotsix_mill.agents.web_tools import (
    make_web_fetch,
    reset_web_fetch_budget,
    reset_trace_web_fetch_budget,
)
from robotsix_mill.config import Settings, Secrets, _reset_secrets


@pytest.fixture(autouse=True)
def _reset_trace_budget():
    """Reset the per-survey-run trace budget before every test so that
    tests that activate it (e.g. ``TestTraceWebFetchBudget``) don't
    leak a non-zero budget into subsequent tests that don't expect one
    (e.g. ``test_web_fetch_cache_hit_is_free``).

    The trace budget is per-process global state in
    ``web_tools._trace_budget_max_calls/_trace_budget_max_bytes`` and
    the corresponding ``_trace_fetch_*`` counters.
    """
    reset_trace_web_fetch_budget(0, 0)


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    # Mirror openrouter_api_key into Secrets so get_secrets() works
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
    # OPENROUTER_API_KEY is now a Secrets-only field; pop before Settings()
    env.pop("OPENROUTER_API_KEY", None)
    return Settings(**env)


# --- agent_references/ folder shipped with the repo ---------------------


def test_repo_ships_agent_references():
    """The real agent_references/ dir is committed and discoverable, so
    the implement agent can read entries when AGENT.md points it there.
    No auto-loading — agents pull on demand."""
    from pathlib import Path

    p = Path("agent_references")
    assert p.is_dir(), "agent_references/ folder is missing from the repo"
    files = sorted(x.name for x in p.glob("*.md"))
    assert "sqlalchemy-sqlite.md" in files, files


# --- web research sub-agent (the cost fix) ------------------------------


def test_web_research_no_key_degrades(tmp_path):
    # No OPENROUTER_API_KEY -> a short message, never an exception.
    s = _settings(tmp_path, OPENROUTER_API_KEY="")
    out = asyncio.run(wr.run_web_research(settings=s, query="anything"))
    assert "unavailable" in out and "OPENROUTER_API_KEY" in out


def test_web_research_subagent_uses_cheap_online_model(tmp_path, monkeypatch):
    """The sub-agent (and only it) builds the cheap level-1 (flash) model
    WITH ":online" and bounds itself by web_research_request_limit. The
    expensive main agent never carries ":online" — web search is delegated
    here."""
    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        web_research_request_limit="5",
    )
    captured = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured["name"] = kw.get("name")

        async def run(self, query, *, usage_limits=None):
            captured["limit"] = usage_limits.request_limit
            captured["query"] = query
            return type("R", (), {"output": "ok"})()

    import pydantic_ai
    from robotsix_mill.agents import base as bmod
    from robotsix_llmio.core.factory import default_tier_config

    def fake_build_openrouter_model(level=1, *, online=False):
        model_name = default_tier_config().for_level(level).model_name
        if online:
            model_name = f"{model_name}:online"
        captured["model"] = model_name
        return object(), object()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(bmod, "build_openrouter_model", fake_build_openrouter_model)

    out = asyncio.run(wr.run_web_research(settings=s, query="q"))
    assert out == "ok"
    # The cheap level-1 model carries the :online surcharge for web search.
    assert captured["model"] == "deepseek/deepseek-v4-flash:online"
    assert captured["limit"] == 5
    assert captured["name"] == "web_research"


def test_compose_prompt_does_not_inject_tool_table(tmp_path):
    """compose_prompt no longer appends a prose tool table — pydantic-ai
    already forwards each tool's signature + docstring as the structured
    ``tools`` array on every API call. The system prompt is the YAML
    body verbatim (plus optional ``## Skills`` when skills are passed)."""
    from robotsix_mill.agents.tool_registry import ToolRegistry

    s = _settings(tmp_path)
    ToolRegistry._tools.clear()
    try:
        p = compose_prompt(s, "BASE PROMPT")
        assert p.strip() == "BASE PROMPT"
        assert "## Available tools" not in p
    finally:
        ToolRegistry._tools.clear()


# --- web_fetch tool -----------------------------------------------------


def test_web_fetch_tool_uses_sandbox_fetch(tmp_path, monkeypatch):
    reset_web_fetch_budget()
    reset_trace_web_fetch_budget(0, 0)
    s = _settings(tmp_path)
    monkeypatch.setattr(sandbox, "fetch", lambda url, *, settings: (0, f"BODY:{url}"))
    wf = make_web_fetch(s)
    assert wf("https://x.test/doc") == "BODY:https://x.test/doc"


def test_web_fetch_tool_reports_errors(tmp_path, monkeypatch):
    reset_web_fetch_budget()
    reset_trace_web_fetch_budget(0, 0)
    s = _settings(tmp_path)

    def boom(url, *, settings):
        raise sandbox.SandboxError("no docker")

    monkeypatch.setattr(sandbox, "fetch", boom)
    assert "fetch error: no docker" in make_web_fetch(s)("https://x.test")


# --- sandbox.fetch container argv (no daemon needed) --------------------


def test_fetch_rejects_non_http(tmp_path):
    s = _settings(tmp_path)
    rc, msg = sandbox.fetch("file:///etc/passwd", settings=s)
    assert rc == 1 and "only http(s)" in msg


def test_fetch_argv_is_locked_down(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=b"hello", stderr=b"")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    rc, body = sandbox.fetch("https://docs.example/api", settings=s)

    a = seen["argv"]
    assert rc == 0 and body == "hello"
    assert a[:3] == ["docker", "run", "--rm"]
    assert "--read-only" in a
    assert a[a.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in a
    assert "-v" not in a  # NO repo/data mount in the fetch container
    assert a[-2:] == ["--", "https://docs.example/api"]  # URL is argv, not shell


def test_fetch_survives_non_utf8_body(tmp_path, monkeypatch):
    """Non-UTF-8 bytes (Latin-1, Shift-JIS, binary) in the curl response
    must NOT crash sandbox.fetch().  Instead, undecodable bytes are
    replaced with \ufffd and the agent gets a usable text string."""
    s = _settings(tmp_path)

    # Invalid UTF-8: 0xff, 0xfe, 0xe9 are all illegal leading bytes
    raw = b"\xff\xfe\xe9 and some text"
    monkeypatch.setattr(
        sandbox.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=raw, stderr=b""),
    )
    rc, body = sandbox.fetch("https://broken.example/encoding", settings=s)
    assert rc == 0
    assert isinstance(body, str)
    # Replacement characters present; valid ASCII tail preserved
    assert "\ufffd" in body
    assert "and some text" in body


def test_fetch_image_config_default():
    """fetch_image defaults to a pinned version, not a mutable tag."""
    s = Settings()
    assert s.fetch_image == "curlimages/curl:8.17.0"


# --- web_fetch: html-to-text extraction --------------------------------------


def test_web_fetch_strips_html_to_prose(tmp_path, monkeypatch):
    """A typical docs-page response (HTML markup wrapping prose) MUST
    come back as whitespace-collapsed text — that's how we shrink a
    315 KB page to ~80 KB so the agent's context doesn't balloon.

    The exact prose content + ordering is what the LLM reads; the
    markup is dead weight.
    """
    from robotsix_mill.agents.web_tools import make_web_fetch, _cache

    _cache.clear()

    html_body = (
        "<!DOCTYPE html><html><head><title>Doc</title>"
        "<style>.x{color:red}</style>"
        "<script>var hidden = 1;</script>"
        "</head><body>"
        "<h1>SQLite transaction control</h1>"
        "<p>The <code>isolation_level</code> attribute controls "
        "transaction behaviour.</p>"
        "</body></html>"
    )
    s = _settings(tmp_path)
    monkeypatch.setattr(sandbox, "fetch", lambda url, *, settings: (0, html_body))
    out = make_web_fetch(s)("https://docs.example/sqlite")

    # The markup is gone — no opening angle brackets remain.
    assert "<" not in out
    # The script payload was dropped wholesale (not just its tags).
    assert "var hidden" not in out
    # The style payload was dropped too.
    assert "color:red" not in out
    # The actual prose is preserved (markdown-ish but readable).
    assert "SQLite transaction control" in out
    assert "isolation_level" in out


def test_web_fetch_passes_non_html_unchanged(tmp_path, monkeypatch):
    """Plain-text bodies (JSON, source files, package metadata)
    have no HTML markers — the extractor leaves them alone so an
    agent fetching a raw .py or .json gets bytes for bytes."""
    from robotsix_mill.agents.web_tools import _cache

    _cache.clear()
    s = _settings(tmp_path)
    raw = '{"name": "blake3", "version": "1.0.5"}'
    monkeypatch.setattr(sandbox, "fetch", lambda url, *, settings: (0, raw))
    out = make_web_fetch(s)("https://pypi.example/blake3/json")
    assert out == raw


def test_web_fetch_raw_mode_disables_extraction(tmp_path, monkeypatch):
    """``web.fetch_raw: true`` returns the verbatim curl body —
    operators who really need markup or are debugging the extractor
    bypass it cleanly."""
    from robotsix_mill.agents.web_tools import _cache

    _cache.clear()
    s = _settings(tmp_path, web_fetch_raw=True)
    body = "<html><body><p>raw</p></body></html>"
    monkeypatch.setattr(sandbox, "fetch", lambda url, *, settings: (0, body))
    assert make_web_fetch(s)("https://x.test/raw") == body


def test_web_fetch_applies_text_cap(tmp_path, monkeypatch):
    """The post-extraction cap bounds what the AGENT sees. The
    network-level ``web_fetch_max_bytes`` is a separate floor; this
    cap is what protects context-token cost when a fetched plain
    file is large but the agent only needs a snippet."""
    from robotsix_mill.agents.web_tools import _cache

    _cache.clear()
    s = _settings(tmp_path, web_fetch_max_text_bytes=100)
    body = "x" * 500
    monkeypatch.setattr(sandbox, "fetch", lambda url, *, settings: (0, body))
    out = make_web_fetch(s)("https://x.test/big")
    # First 100 chars present, then the truncation marker.
    assert out.startswith("x" * 100)
    assert "[... description truncated; 400 chars omitted]" in out


# --- web_fetch: per-run URL dedupe -------------------------------------------


def test_web_fetch_dedupes_fragment_only_variants(tmp_path, monkeypatch):
    """Two URLs differing only in ``#fragment`` are the SAME page —
    the cache MUST return the prior result without a second sandbox
    spawn. Trace d40e3c9 (refine f6e2) cost an extra $0.25 because
    the agent fetched the Python sqlite3 docs page twice for two
    different anchors. This guards the fix."""
    from robotsix_mill.agents.web_tools import _cache

    _cache.clear()
    s = _settings(tmp_path)

    calls: list[str] = []

    def fake_fetch(url, *, settings):
        calls.append(url)
        return 0, f"plain body for {url}"

    monkeypatch.setattr(sandbox, "fetch", fake_fetch)
    wf = make_web_fetch(s)
    a = wf("https://docs.python.org/3/library/sqlite3.html#transaction-control")
    b = wf(
        "https://docs.python.org/3/library/sqlite3.html#sqlite3-connection-autocommit"
    )
    # ONE underlying fetch — the second was served from the cache.
    assert len(calls) == 1
    # Both calls return the same processed body.
    assert a == b


def test_web_fetch_does_not_dedupe_different_paths(tmp_path, monkeypatch):
    """The dedupe key includes path + query, so genuinely different
    URLs still go through to the sandbox."""
    from robotsix_mill.agents.web_tools import _cache

    _cache.clear()
    s = _settings(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        sandbox,
        "fetch",
        lambda url, *, settings: calls.append(url) or (0, "body"),
    )
    wf = make_web_fetch(s)
    wf("https://docs.example/a")
    wf("https://docs.example/b")
    assert len(calls) == 2


def test_web_fetch_raw_mode_bypasses_cache(tmp_path, monkeypatch):
    """When raw-mode is on, every call hits the sandbox — the
    escape hatch must NOT silently serve an extracted prior result.
    Operator opted in to verbatim bytes; that's what they get."""
    from robotsix_mill.agents.web_tools import _cache

    _cache.clear()
    s = _settings(tmp_path, web_fetch_raw=True)
    calls = []
    monkeypatch.setattr(
        sandbox,
        "fetch",
        lambda url, *, settings: calls.append(url) or (0, "<html>x</html>"),
    )
    wf = make_web_fetch(s)
    wf("https://x.test/raw#a")
    wf("https://x.test/raw#b")
    assert len(calls) == 2


# --- web_fetch: per-consult fetch budget -------------------------------------


def test_web_fetch_call_count_cap(tmp_path, monkeypatch):
    """After N = web_fetch_max_calls real fetches, the (N+1)-th call
    returns the budget-exhausted sentinel WITHOUT calling sandbox.fetch.
    This is the cap that bounds the runaway fetch fan-out a single
    consult could otherwise drive (specimen: 63 fetches → ~1.9M tokens)."""
    from robotsix_mill.agents.web_tools import _cache, reset_web_fetch_budget

    _cache.clear()
    reset_web_fetch_budget()
    s = _settings(tmp_path, web_fetch_max_calls=2)

    calls: list[str] = []

    def fake_fetch(url, *, settings):
        calls.append(url)
        return 0, f"body for {url}"

    monkeypatch.setattr(sandbox, "fetch", fake_fetch)
    wf = make_web_fetch(s)

    # Two distinct URLs consume the budget.
    assert wf("https://x.test/a") == "body for https://x.test/a"
    assert wf("https://x.test/b") == "body for https://x.test/b"
    assert len(calls) == 2

    # The third distinct URL is refused without a sandbox fetch.
    out = wf("https://x.test/c")
    assert "budget exhausted" in out
    assert len(calls) == 2  # sandbox.fetch NOT invoked past the cap


def test_web_fetch_byte_ceiling(tmp_path, monkeypatch):
    """Once cumulative returned-text bytes reach web_fetch_max_total_bytes
    (>0), the next real fetch returns the sentinel without fetching."""
    from robotsix_mill.agents.web_tools import _cache, reset_web_fetch_budget

    _cache.clear()
    reset_web_fetch_budget()
    # Generous call cap so the BYTE ceiling is what trips.
    s = _settings(
        tmp_path,
        web_fetch_max_calls=100,
        web_fetch_max_total_bytes=500,
    )

    calls: list[str] = []

    def fake_fetch(url, *, settings):
        calls.append(url)
        return 0, "x" * 400  # plain text, no extraction

    monkeypatch.setattr(sandbox, "fetch", fake_fetch)
    wf = make_web_fetch(s)

    # First fetch (400 bytes) is under the ceiling and returns the body.
    assert wf("https://x.test/a") == "x" * 400
    # Cumulative now 400; still under 500 so the next fetch happens and
    # pushes us to 800 (>= 500).
    assert wf("https://x.test/b") == "x" * 400
    assert len(calls) == 2

    # Cumulative bytes (800) >= ceiling (500) → next call refused.
    out = wf("https://x.test/c")
    assert "budget exhausted" in out
    assert len(calls) == 2


def test_web_fetch_byte_ceiling_zero_disables(tmp_path, monkeypatch):
    """A web_fetch_max_total_bytes of 0 disables the byte ceiling — only
    the call-count cap applies."""
    from robotsix_mill.agents.web_tools import _cache, reset_web_fetch_budget

    _cache.clear()
    reset_web_fetch_budget()
    s = _settings(
        tmp_path,
        web_fetch_max_calls=100,
        web_fetch_max_total_bytes=0,
    )

    calls: list[str] = []

    def fake_fetch(url, *, settings):
        calls.append(url)
        return 0, "x" * 10_000

    monkeypatch.setattr(sandbox, "fetch", fake_fetch)
    wf = make_web_fetch(s)

    # Many large fetches, none refused — the byte ceiling is off.
    for i in range(5):
        assert wf(f"https://x.test/{i}") == "x" * 10_000
    assert len(calls) == 5


def test_web_fetch_cache_hit_is_free(tmp_path, monkeypatch):
    """Cache hits do NOT consume the budget. With cap=1, fetching the
    SAME canonical URL twice within the TTL must return the body both
    times — the second call is served from _cache and never charged."""
    from robotsix_mill.agents.web_tools import (
        _cache,
        reset_web_fetch_budget,
        web_fetch_budget,
    )

    _cache.clear()
    reset_web_fetch_budget()
    s = _settings(tmp_path, web_fetch_max_calls=1)

    calls: list[str] = []

    def fake_fetch(url, *, settings):
        calls.append(url)
        return 0, f"body for {url}"

    monkeypatch.setattr(sandbox, "fetch", fake_fetch)
    wf = make_web_fetch(s)

    first = wf("https://x.test/page")
    second = wf("https://x.test/page")
    assert first == second == "body for https://x.test/page"
    # Only ONE real fetch; the second was a cache hit.
    assert len(calls) == 1
    # Budget charged exactly once.
    assert web_fetch_budget()[0] == 1


def test_web_fetch_raw_mode_is_free(tmp_path, monkeypatch):
    """web_fetch_raw=true returns do NOT consume the budget — the
    operator escape hatch is exempt."""
    from robotsix_mill.agents.web_tools import (
        _cache,
        reset_web_fetch_budget,
        web_fetch_budget,
    )

    _cache.clear()
    reset_web_fetch_budget()
    s = _settings(tmp_path, web_fetch_raw=True, web_fetch_max_calls=1)

    calls: list[str] = []
    monkeypatch.setattr(
        sandbox,
        "fetch",
        lambda url, *, settings: calls.append(url) or (0, "raw body"),
    )
    wf = make_web_fetch(s)

    # Several raw fetches, never refused, never charged.
    assert wf("https://x.test/a") == "raw body"
    assert wf("https://x.test/b") == "raw body"
    assert wf("https://x.test/c") == "raw body"
    assert len(calls) == 3
    assert web_fetch_budget() == (0, 0)


def test_reset_web_fetch_budget_zeroes_counters(tmp_path, monkeypatch):
    """reset_web_fetch_budget() zeroes the counters so a fresh consult
    starts clean — even after a prior consult exhausted the cap."""
    from robotsix_mill.agents.web_tools import (
        _cache,
        reset_web_fetch_budget,
        web_fetch_budget,
    )

    _cache.clear()
    reset_web_fetch_budget()
    s = _settings(tmp_path, web_fetch_max_calls=1)
    monkeypatch.setattr(
        sandbox, "fetch", lambda url, *, settings: (0, f"body for {url}")
    )
    wf = make_web_fetch(s)

    wf("https://x.test/a")
    assert web_fetch_budget()[0] == 1
    # Next distinct URL would be refused...
    assert "budget exhausted" in wf("https://x.test/b")

    # ...until the budget is reset.
    reset_web_fetch_budget()
    assert web_fetch_budget() == (0, 0)
    assert wf("https://x.test/c") == "body for https://x.test/c"


def test_run_web_knowledge_resets_budget(tmp_path, monkeypatch):
    """run_web_knowledge resets the fetch budget once at the start of
    each consult so every web_research sub-agent it fans out to shares
    one fresh allowance. Stub the pydantic_ai Agent so no real model
    request is made."""
    from robotsix_mill.agents import web_tools
    from robotsix_mill.agents.web_knowledge import run_web_knowledge

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    reset_calls = {"n": 0}
    real_reset = web_tools.reset_web_fetch_budget

    def counting_reset():
        reset_calls["n"] += 1
        real_reset()

    monkeypatch.setattr(web_tools, "reset_web_fetch_budget", counting_reset)

    class FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, question, *, usage_limits=None):
            return type("R", (), {"output": "the answer"})()

    import pydantic_ai
    from robotsix_mill.agents import base as bmod

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "build_openrouter_model",
        lambda level=1, *, online=False: (object(), object()),
    )

    out = asyncio.run(run_web_knowledge(settings=s, question="anything"))
    assert out == "the answer"
    assert reset_calls["n"] == 1


# --- web_fetch: per-survey-run trace budget --------------------------------


class TestTraceWebFetchBudget:
    """Per-survey-run web_fetch budget — trace-level caps that survive
    per-consult budget resets."""

    def test_trace_web_fetch_call_cap(self, tmp_path, monkeypatch):
        """After reset_trace_web_fetch_budget(3, ...), the 4th cache-miss
        web_fetch call returns a budget-exhausted sentinel even after
        reset_web_fetch_budget() (per-consult reset does NOT clear trace
        counters)."""
        from robotsix_mill.agents.web_tools import (
            _cache,
            reset_web_fetch_budget,
            reset_trace_web_fetch_budget,
        )

        _cache.clear()
        reset_web_fetch_budget()
        reset_trace_web_fetch_budget(3, 1_000_000)

        s = _settings(
            tmp_path,
            web_fetch_max_calls=100,
            web_fetch_max_total_bytes=0,
        )

        calls: list[str] = []

        def fake_fetch(url, *, settings):
            calls.append(url)
            return 0, f"body for {url}"

        monkeypatch.setattr(sandbox, "fetch", fake_fetch)
        wf = make_web_fetch(s)

        # First 3 distinct URLs should succeed.
        assert wf("https://x.test/a") == "body for https://x.test/a"
        assert wf("https://x.test/b") == "body for https://x.test/b"
        assert wf("https://x.test/c") == "body for https://x.test/c"
        assert len(calls) == 3

        # Reset per-consult budget — trace counters should survive.
        reset_web_fetch_budget()

        # 4th distinct URL should be refused by trace budget.
        out = wf("https://x.test/d")
        assert "trace budget exhausted" in out.lower()
        assert len(calls) == 3  # no new fetch

    def test_trace_web_fetch_byte_cap(self, tmp_path, monkeypatch):
        """Cumulative bytes across multiple consults hit the trace byte
        ceiling."""
        from robotsix_mill.agents.web_tools import (
            _cache,
            reset_web_fetch_budget,
            reset_trace_web_fetch_budget,
        )

        _cache.clear()
        reset_web_fetch_budget()
        reset_trace_web_fetch_budget(100, 500)

        s = _settings(
            tmp_path,
            web_fetch_max_calls=100,
            web_fetch_max_total_bytes=0,  # per-consult byte ceiling off
        )

        calls: list[str] = []

        def fake_fetch(url, *, settings):
            calls.append(url)
            return 0, "x" * 300

        monkeypatch.setattr(sandbox, "fetch", fake_fetch)
        wf = make_web_fetch(s)

        # First fetch: 300 bytes, under 500.
        assert wf("https://x.test/a") == "x" * 300
        assert len(calls) == 1

        # Second fetch: cumulative 600 > 500 limit.  The call cap
        # check is pre-fetch, but the byte ceiling is enforced
        # post-fetch (after the body is processed) so the sandbox
        # call still fires but the body is rejected.
        out = wf("https://x.test/b")
        assert "trace budget exhausted" in out.lower()
        assert len(calls) == 2  # sandbox call fired; body rejected post-fetch

        # Per-consult reset doesn't help.
        reset_web_fetch_budget()
        out = wf("https://x.test/c")
        assert "trace budget exhausted" in out.lower()

    def test_trace_budget_inactive_when_not_set(self, tmp_path, monkeypatch):
        """When reset_trace_web_fetch_budget has never been called (or
        called with max_calls=0), the trace budget is a no-op — only the
        per-consult budget gates."""
        from robotsix_mill.agents.web_tools import (
            _cache,
            reset_web_fetch_budget,
            reset_trace_web_fetch_budget,
        )

        _cache.clear()
        reset_web_fetch_budget()
        # Call with all zeros — should deactivate.
        reset_trace_web_fetch_budget(0, 0)

        s = _settings(
            tmp_path,
            web_fetch_max_calls=2,
            web_fetch_max_total_bytes=0,
        )

        calls: list[str] = []

        def fake_fetch(url, *, settings):
            calls.append(url)
            return 0, f"body for {url}"

        monkeypatch.setattr(sandbox, "fetch", fake_fetch)
        wf = make_web_fetch(s)

        # Per-consult budget gates at 2.
        assert wf("https://x.test/a") == "body for https://x.test/a"
        assert wf("https://x.test/b") == "body for https://x.test/b"
        out = wf("https://x.test/c")
        assert "budget exhausted" in out  # per-consult, NOT trace
        assert "trace" not in out.lower()

    def test_reset_trace_web_fetch_budget_zeroes_counters(self, tmp_path, monkeypatch):
        """Calling reset_trace_web_fetch_budget mid-run zeroes the trace
        counters."""
        from robotsix_mill.agents.web_tools import (
            _cache,
            reset_web_fetch_budget,
            reset_trace_web_fetch_budget,
        )

        _cache.clear()
        reset_web_fetch_budget()
        reset_trace_web_fetch_budget(2, 1_000_000)

        s = _settings(
            tmp_path,
            web_fetch_max_calls=100,
            web_fetch_max_total_bytes=0,
        )

        calls: list[str] = []

        def fake_fetch(url, *, settings):
            calls.append(url)
            return 0, f"body for {url}"

        monkeypatch.setattr(sandbox, "fetch", fake_fetch)
        wf = make_web_fetch(s)

        # Consume the trace budget.
        assert wf("https://x.test/a") == "body for https://x.test/a"
        assert wf("https://x.test/b") == "body for https://x.test/b"
        out = wf("https://x.test/c")
        assert "trace budget exhausted" in out.lower()

        # Reset the trace budget — counters zeroed.
        reset_trace_web_fetch_budget(2, 1_000_000)

        # Now we can fetch again.
        assert wf("https://x.test/d") == "body for https://x.test/d"
        assert wf("https://x.test/e") == "body for https://x.test/e"
