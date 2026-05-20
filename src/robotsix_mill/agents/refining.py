"""The refine agent: a capable model that authors the spec, grounded
in the ACTUAL repo when a local clone is available.

When the refine stage has cloned the target repo it passes
``repo_dir``; the agent then gets the cheap ``explore`` scout +
read-only ``read_file``/``list_dir``/``run_command`` to ground the
spec in real code (instead of web-fetching the project's own files —
slow & indirect). ``run_command`` runs sandboxed, read-only commands
(e.g. re-run failing tests when error output is truncated).
``web_research`` stays for genuinely external lookups only. With no
repo (no forge configured) it falls back to draft-only as before.
``run_refine_agent`` is the seam tests monkeypatch.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings

SYSTEM_PROMPT = """\
You turn a rough ticket draft into a precise, self-contained
engineering spec an autonomous coder can implement without asking
questions.

- If a repo is available you have `explore` (a scout returning
  concise paths/symbols/line-ranges, never whole files),
  `read_file`/`list_dir`, and `run_command`. USE THEM to ground the
  spec in the ACTUAL codebase — real file paths, existing
  patterns/conventions, and constraints. Do NOT web-fetch the
  project's own files.
- Use `run_command` to re-run failing tests when a traceback is
  truncated or to inspect runtime behaviour (e.g. `pytest
  tests/test_foo.py -x --tb=long`, `python -c "import module; …"`,
  linters).  The sandbox is read-only; you cannot mutate the repo.
- When the ticket has ``artifacts/evidence.txt`` (check with
  ``read_file``), incorporate its contents (e.g. the exact failing
  command, stdout/stderr, traceback) into the refined spec so the
  implement agent has the raw evidence to cross-check.
- Use `web_research` ONLY for things not in the repo (a
  library/API/standard/best practice). Skip it when unneeded.
- The <draft> section may be empty (the user may have only provided a
  title). In that case, derive the spec from the title's intent alone.
- Stay faithful to the draft's intent; invent nothing unrelated. Be
  concrete and testable.

## Output format — JSON envelope (not raw Markdown)

You MUST output a single JSON object, no preamble, no fences.

When the draft describes ONE focused change, output:

{"split": false, "spec": "## Problem\n...\n## Scope\n...\n## Acceptance criteria\n...\n## Out of scope / constraints\n..."}

When the draft bundles MULTIPLE independent, self-contained changes
that can each ship alone, split into focused children:

{"split": true, "children": [
  {"title": "Short title for change A", "spec_markdown": "## Problem\n...", "depends_on": []},
  {"title": "Short title for change B", "spec_markdown": "## Problem\n...", "depends_on": [0]}
]}

Rules for splitting:
- **Conservative splitting**: only split when the draft genuinely
  contains multiple independently-shippable changes.  A borderline
  draft MUST stay as one spec.  Over-splitting is as bad as
  over-bundling.
- Each child's ``spec_markdown`` must be a complete, self-contained
  spec with ## Problem, ## Scope, ## Acceptance criteria, and
  ## Out of scope / constraints sections.
- ``depends_on`` is a list of zero-based indices of earlier children
  in the same split that must be completed first.  Use only when
  child B genuinely builds on child A (e.g. a helper added then used).
  Sequential work that could be parallelised should have empty
  ``depends_on``.
- The union of all children's scope must faithfully cover the entire
  original draft — nothing dropped, nothing added.
"""


def run_refine_agent(
    *,
    settings: Settings,
    title: str,
    draft: str,
    repo_dir: Path | None = None,
    reviewer_comments: str | None = None,
) -> dict:
    """Return a structured dict. When ``repo_dir`` is given the agent
    grounds the spec in that local clone via explore/read_file/
    list_dir/run_command; otherwise it works draft-only. When
    ``reviewer_comments`` is given the agent incorporates the feedback
    into the refined spec. Raises RuntimeError if no OpenRouter key is
    configured (build_agent enforces this).

    Return shape:
      - Normal single-scope: ``{"split": false, "spec": "## Problem\\n..."}``
      - Multi-scope split: ``{"split": true, "children": [{"title": "...",
        "spec_markdown": "...", "depends_on": [0]}, ...]}``
    """
    from .base import build_agent
    from .retry import call_with_retry

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir", "run_command")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    # Tech-specific gotchas are NOT injected into refine — they live
    # under agent_references/ and the implement agent consults them
    # on-demand via the pointer in AGENT.md when it actually touches
    # the relevant stack. Keeps refine's prompt small and prevents the
    # spec writer from prescribing fixes for traps it can't verify.
    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
        web=True,  # cheap web_research sub-agent (external lookups only)
        model_name=settings.refine_model,
        name="refine",
    )

    # Build user prompt: title, draft, and optionally reviewer feedback.
    user_prompt = f"<title>{title}</title>\n<draft>\n{draft}\n</draft>"
    if reviewer_comments:
        user_prompt += (
            "\n<reviewer_feedback>The reviewer sent this spec back "
            "with the following comments. Address each one in the "
            "revised spec:\n\n"
            f"{reviewer_comments}\n</reviewer_feedback>"
        )

    result = call_with_retry(
        lambda: agent.run_sync(user_prompt),
        settings=settings, what="refine",
    )
    raw = str(result.output).strip()
    return _parse_agent_json(raw)


def _parse_agent_json(raw: str) -> dict:
    """Parse the agent's JSON envelope, with graceful fallback.

    If the model outputs raw Markdown (no JSON envelope), wrap it as a
    single-scope spec.  If JSON is present but malformed, also fall back.
    """
    import json

    cleaned = raw.strip()

    # Try to find the outermost JSON object by looking for {"split"
    # and then tracking brace depth.
    split_idx = cleaned.find('{"split"')
    if split_idx == -1:
        split_idx = cleaned.find('{\n  "split"')
    if split_idx == -1:
        split_idx = cleaned.find('{\n "split"')
    if split_idx == -1:
        split_idx = cleaned.find('{ "split"')

    if split_idx >= 0:
        # Track brace depth to find the matching close brace.
        depth = 0
        in_string = False
        escape = False
        end_idx = -1
        for i in range(split_idx, len(cleaned)):
            c = cleaned[i]
            if escape:
                escape = False
                continue
            if c == '\\' and in_string:
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break

        if end_idx > split_idx:
            json_candidate = cleaned[split_idx:end_idx]
            try:
                parsed = json.loads(json_candidate)
                if isinstance(parsed, dict) and "split" in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass

    # Fallback: treat the entire output as a single-scope Markdown spec.
    return {"split": False, "spec": raw}
