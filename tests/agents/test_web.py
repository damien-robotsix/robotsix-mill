import subprocess


from robotsix_mill import sandbox
from robotsix_mill.agents import web_research as wr
from robotsix_mill.agents.base import compose_prompt, _model_name
from robotsix_mill.agents.web_research import make_web_research_tool
from robotsix_mill.agents.web_tools import make_web_fetch
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    # Mirror openrouter_api_key into Secrets so get_secrets() works
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
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


# --- model id / prompt composition (no key, no pydantic_ai needed) ------


def test_main_model_never_online(tmp_path):
    # The expensive main agent must never carry ":online" — web search
    # is delegated to the cheap sub-agent. (No "openrouter:" prefix: the
    # provider is set explicitly for the cost-instrumented model.)
    s = _settings(tmp_path, model="x/y")
    assert _model_name(s) == "x/y"
    assert ":online" not in _model_name(s)
    # even with web search enabled (the default) the main model is plain
    s2 = _settings(tmp_path, model="x/y", web_search="true")
    assert _model_name(s2) == "x/y"


# --- web research sub-agent (the cost fix) ------------------------------


def test_web_research_tool_delegates_to_seam(tmp_path, monkeypatch):
    """The tool exposed to the main agent must return EXACTLY the
    sub-agent seam's conclusion string (no raw pages leak through)."""
    s = _settings(tmp_path)
    seen = {}

    def fake(*, settings, query):
        seen["query"] = query
        return f"CONCLUSION about {query}"

    monkeypatch.setattr(wr, "run_web_research", fake)
    tool = make_web_research_tool(s)
    assert tool("python 3.14 release date") == (
        "CONCLUSION about python 3.14 release date"
    )
    assert seen["query"] == "python 3.14 release date"


def test_web_research_no_key_degrades(tmp_path):
    # No OPENROUTER_API_KEY -> a short message, never an exception.
    s = _settings(tmp_path, OPENROUTER_API_KEY="")
    out = wr.run_web_research(settings=s, query="anything")
    assert "unavailable" in out and "OPENROUTER_API_KEY" in out


def test_web_research_subagent_uses_cheap_online_model(tmp_path, monkeypatch):
    """The sub-agent (and only it) builds the cheap model WITH ":online"
    and bounds itself by web_research_request_limit."""
    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        web_research_model="cheap/mini",
        web_research_request_limit="5",
    )
    captured = {}

    class FakeModel:
        def __init__(self, name, **kw):
            captured["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            captured["name"] = kw.get("name")

        def run_sync(self, query, *, usage_limits=None):
            captured["limit"] = usage_limits.request_limit
            captured["query"] = query
            return type("R", (), {"output": "ok"})()

    import pydantic_ai
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    out = wr.run_web_research(settings=s, query="q")
    assert out == "ok"
    assert captured["model"] == "cheap/mini:online"
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
    s = _settings(tmp_path)
    monkeypatch.setattr(sandbox, "fetch", lambda url, *, settings: (0, f"BODY:{url}"))
    wf = make_web_fetch(s)
    assert wf("https://x.test/doc") == "BODY:https://x.test/doc"


def test_web_fetch_tool_reports_errors(tmp_path, monkeypatch):
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
        return subprocess.CompletedProcess(argv, 0, stdout="hello", stderr="")

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
    # First 100 chars present, then the marker.
    assert out.startswith("x" * 100)
    assert "[truncated, fetched 500 chars total" in out


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


# --- web_fetch: html_to_text helper ------------------------------------------


def test_html_to_text_drops_scripts_and_styles():
    """Scripts and styles are removed wholesale (content + tags) so
    an LLM doesn't have to read JavaScript or CSS. They're dead
    weight in every doc page we fetch."""
    from robotsix_mill.agents.web_tools import html_to_text

    body = (
        "<html><body>"
        "<script>alert('x'); var leaked = 'data';</script>"
        "<style>body { color: red; }</style>"
        "<p>Hello world</p>"
        "</body></html>"
    )
    out = html_to_text(body)
    assert "alert" not in out
    assert "leaked" not in out
    assert "color: red" not in out
    assert "Hello world" in out


def test_html_to_text_unescapes_entities():
    """``&amp;`` → ``&`` and ``&nbsp;`` → space — the agent reads the
    rendered text, not the source-level entity references."""
    from robotsix_mill.agents.web_tools import html_to_text

    out = html_to_text("<p>foo &amp; bar&nbsp;baz</p>")
    assert "&" in out
    # &nbsp; came through as a real space; the result has no
    # entity reference text.
    assert "&nbsp;" not in out
    assert "foo & bar" in out


def test_html_to_text_collapses_whitespace():
    """Removing tags inserts runs of whitespace. The extractor
    collapses them so the agent doesn't see paragraphs of newlines
    between every word."""
    from robotsix_mill.agents.web_tools import html_to_text

    body = "<div><p>one</p>\n\n\n<p>two</p></div>"
    out = html_to_text(body)
    # At most one blank line between paragraphs.
    assert "\n\n\n" not in out
    assert "one" in out
    assert "two" in out
