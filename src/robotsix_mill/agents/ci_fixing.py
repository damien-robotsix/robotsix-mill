"""CI-fix agent: auto-fixes failing remote CI checks on a PR branch.

Reads the failing check-run summary/details from the forge, inspects
the affected files in the ticket's workspace clone, makes the minimal
code change to fix the failure, runs the project's local tests, and
commits. Returns ``True`` iff the fix was applied successfully.

This agent operates *only* on the local clone — it never pushes, opens
PRs, or interacts with the forge.  The caller (ci_fix stage) decides
whether to force-push the result.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings


def run_ci_fix_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    branch: str,
    failing_summary: str,
) -> bool:
    """Run one CI-fix attempt based on *failing_summary*.

    Uses the LLM (pydantic-ai agent) with sandboxed file + shell tools
    scoped to *repo_dir*.  The agent reads the failing summary, inspects
    the relevant files, makes minimal edits, runs local tests, and
    commits.  Returns ``True`` only when the fix succeeds.  One
    invocation = one attempt.

    This is the mockable seam — tests monkeypatch it to avoid real LLM
    and Docker calls.
    """
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from .base import build_agent
    from .fs_tools import build_fs_tools

    # Build tools confined to the ticket's own clone.
    tools = build_fs_tools(Path(repo_dir), settings)

    system_prompt = f"""You are a CI-fix specialist. Your ONLY job is to fix
failing remote CI checks on a PR branch. The failure may be ANY kind of
CI failure — not just tests.  For example:

- Workflow YAML issues (invalid syntax, bad action pin, missing
  ``permissions:`` block)
- Docker build errors (``docker build`` / ``docker-compose`` failures)
- Lint or type-check failures (ruff, mypy, eslint, etc.)
- Dependency vulnerability / CVE security gates (Trivy, Snyk, etc.)
- Build or compilation errors
- Test failures (any framework: pytest, cargo test, npm test, etc.)

The failing check summary is provided below — it may include job logs
under a ``**Job logs:**`` section.  Use it to understand what is broken,
then:

1. Use read_file to inspect the failing files (within {repo_dir} only).
2. Use write_file to make the **minimal code change** to fix the failure.
3. Run the project's test/verify command to confirm the fix:
   - Infer the right command from the project structure (e.g. pytest,
     ``npm test``, ``make test``, ``cargo test``, ``ruff check``,
     ``mypy``, ``docker build .``, ``pre-commit run``, etc.).
   - Look at the project's files (pyproject.toml, Makefile, package.json,
     Cargo.toml, Dockerfile, .pre-commit-config.yaml, etc.) to decide.
4. If the verify command passes, commit:
   ``git add -A && git commit -q -m "ci: auto-fix <brief description>"``
5. Report DONE.

IMPORTANT RULES:
- NEVER change unrelated code — only the minimum needed to fix the CI
  failure.
- NEVER push, fetch other remotes, or touch any branch other than the
  current ticket branch ({branch}).
- NEVER run destructive git commands (reset --hard, rebase, etc.).

**NO GATE WEAKENING — you must NEVER weaken any security, lint, or
quality gate.** Specifically forbidden:
- Lowering a severity threshold (e.g. ``severity: CRITICAL`` →
  ``HIGH``) in Trivy/Snyk config.
- Removing or raising ``exit-code`` settings in workflow YAML.
- Setting ``continue-on-error: true`` to bypass a failing step.
- Removing a linter rule to silence a legitimate finding.
- Commenting out a check or step to make CI pass.

Instead, you MUST use the documented exception path:
- For Trivy: add entries to ``.trivyignore`` with a ``# justification: …``
  comment explaining why the CVE is a false positive or accepted risk.
- For linters: use inline ``# noqa`` / ``# type: ignore`` / ``<!-- eslint-disable -->``
  comments with a brief reason.
- For Docker build errors: fix the ``Dockerfile`` or dependency
  version, not the build command.
- For permission errors: add the minimum required ``permissions:`` block.

If the failure cannot be resolved (e.g. flaky infra test, missing
secrets, deeper design issue), report FAILED with a short reason.

After the fix completes (or you determine it cannot), respond with
EXACTLY one word on its own line: DONE or FAILED.  You may add a brief
explanation after FAILED."""

    agent = build_agent(
        settings,
        system_prompt=system_prompt,
        output_type=str,
        tools=tools,
        web=False,
        name="ci_fix",
    )

    result = agent.run_sync(
        f"CI is failing on branch '{branch}' in {repo_dir}. "
        "Here is the failing check summary:\n\n"
        f"```\n{failing_summary}\n```\n\n"
        "Follow the system prompt exactly.",
    )

    output = str(result.output or "").strip()
    return output.upper().startswith("DONE")
