"""Review stage: CODE_REVIEW -> DOCUMENTING | READY | AWAITING_USER_REPLY.

Runs a blind dual-model review of the implementation diff. The review
agent sees ONLY the git diff and ticket spec — no implementation
context.  APPROVE → DOCUMENTING; REQUEST_CHANGES → READY (with review
comments stored); NEEDS_DISCUSSION → AWAITING_USER_REPLY (posts the
verdict as an [ASK_USER] thread; operator's reply auto-resumes review).
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
from pathlib import Path

from ..agents.reviewing import ReviewAsk, ReviewVerdict, run_review_agent
from ..config import target_branch_for
from ..config.repos import get_repos_config
from ..config.settings import Settings
from ..core.models import Ticket
from ..core.states import State
from ..core.workspace import Workspace
from ..forge.auth import github_token
from ..forge.github import _parse_owner_repo
from ..vcs import git_ops
from ._implemented_repos import ImplementedRepo, combined_diff, implemented_repos
from .base import Outcome, Stage, StageContext
from .implement._shared import (
    _is_config_only_change,
    _is_rename_only_change,
    _is_small_mechanical_refactor,
)
from .refine.helpers import verify_claim

log = logging.getLogger("robotsix_mill.stages.review")


def _collapse_comments(comments: str) -> str:
    """Collapse and truncate reviewer comments for the ``review.md`` artifact.

    Rules:
    - Replace internal newlines with ``" / "``.
    - Strip leading/trailing whitespace.
    - Truncate to 300 chars; append ``"…"`` when truncated.
    - When empty/whitespace-only, return ``"(no details)"``.
    """
    collapsed = comments.replace("\n", " / ").strip()
    if not collapsed:
        return "(no details)"
    if len(collapsed) > 300:
        return collapsed[:300] + "…"
    return collapsed


def _sanitize_comments(text: str) -> str:
    """Strip leading [ASK_USER] markers from agent-written review comments.

    The review agent occasionally writes [ASK_USER] in its comments field
    for APPROVE or REQUEST_CHANGES verdicts, but only NEEDS_DISCUSSION
    should produce [ASK_USER] threads (the system adds the prefix there).
    """
    return re.sub(r"^\[ASK_USER\]\s*", "", text)


_WORKFLOW_RE = re.compile(
    r"uses:\s*([^/\s]+(?:/(?!\.github/(?:workflows|actions)/)[^/\s]+)?)"
    r"/\.github/(?:workflows|actions)/[^@\s]+",
    re.IGNORECASE,
)


def _workflow_refs_from_diff(diff: str) -> set[str]:
    """Extract ``owner/repo`` references from reusable-workflow ``uses:`` lines.

    Matches external references (``uses: owner/repo/.github/workflows/...``
    or single-org shorthand ``uses: org/.github/workflows/...``).  Relative
    paths (``./``) and Docker references (``docker://...``) are ignored.
    Returns a deduplicated set.

    >>> _workflow_refs_from_diff('uses: my-org/my-repo/.github/workflows/ci.yml@v1')
    {'my-org/my-repo'}
    >>> _workflow_refs_from_diff('uses: robotsix-mill/.github/workflows/deps-bump.yml@main')
    {'robotsix-mill'}
    >>> _workflow_refs_from_diff('uses: ./github/workflows/local.yml')
    set()
    """
    refs: set[str] = set()
    for m in _WORKFLOW_RE.finditer(diff):
        refs.add(m.group(1))
    return refs


_ACTION_USES_RE = re.compile(
    r"uses:\s*"
    r"([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+(?:/[^@#\s/]+)*)"
    r"@(\S+)"
    r"(?:\s*#\s*(.*))?",
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# owner/repo format expected by _verify_action_sha — must be two
# dot-joined segments of alphanumeric, dot, underscore, and hyphen
# characters (e.g. "actions/checkout", "github/codeql-action").
_OWNER_REPO_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")


def _action_refs_from_diff(diff: str) -> list[tuple[str, str, str, str]]:
    r"""Extract action ``uses:`` references from added diff lines.

    Scans ``^\\+`` lines (excluding the ``+++`` header) for ``uses:``
    directives of the form ``uses: <owner>/<repo>[/<subpath>]@<ref>``.
    Skips local (``./``), Docker (``docker://``), and reusable-workflow
    refs (those containing ``/.github/workflows/`` or
    ``/.github/actions/`` — already handled by
    :func:`_workflow_refs_from_diff`).

    Returns ``[(file_path, action_slug, ref, comment), ...]`` where
    *comment* is the trailing ``# <version>`` text (empty string when
    absent).

    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\\n'
    ...     '+    uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2\\n'
    ... )
    [('.github/workflows/ci.yml', 'actions/checkout', '11bd71901bbe5b1630ceea73d27597364c9af683', 'v4.2.2')]

    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\\n'
    ...     '+    uses: actions/checkout@v4\\n'
    ... )
    [('.github/workflows/ci.yml', 'actions/checkout', 'v4', '')]

    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\\n'
    ...     '+    uses: github/codeql-action/init@6b0550b4a2a7c00e939e5501b0c0b3f654b3d8e4 # v3.29.2\\n'
    ... )
    [('.github/workflows/ci.yml', 'github/codeql-action/init', '6b0550b4a2a7c00e939e5501b0c0b3f654b3d8e4', 'v3.29.2')]

    >>> # Local refs are skipped.
    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\\n'
    ...     '+    uses: ./.github/actions/my-action@main\\n'
    ... )
    []

    >>> # Docker refs are skipped.
    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\\n'
    ...     '+    uses: docker://ubuntu:latest\\n'
    ... )
    []

    >>> # Reusable-workflow refs are skipped (already handled by _WORKFLOW_RE).
    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\\n'
    ...     '+    uses: my-org/my-repo/.github/workflows/ci.yml@v1\\n'
    ... )
    []

    >>> # +++ header lines are not scanned.
    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\\n'
    ... )
    []

    >>> # Deleted lines (^-prefixed) are not scanned.
    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\\n'
    ...     '-    uses: evilcorp/backdoor@v1\\n'
    ...     '+    uses: actions/checkout@v4\\n'
    ... )
    [('.github/workflows/ci.yml', 'actions/checkout', 'v4', '')]
    """
    results: list[tuple[str, str, str, str]] = []
    current_file: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/") and not line.startswith("+++ b/dev/null"):
            current_file = line[6:].strip()
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if current_file is None:
            continue
        if not current_file.startswith(".github/"):
            continue  # Not a workflow file — skip matches
        m = _ACTION_USES_RE.search(line)
        if not m:
            continue
        slug, ref, comment = m.group(1), m.group(2), (m.group(3) or "").strip()
        # Skip local refs.
        if slug.startswith("./"):
            continue
        # Skip Docker refs.
        if slug.startswith("docker://"):
            continue
        # Skip reusable-workflow refs already handled by _workflow_refs_from_diff.
        if "/.github/workflows/" in slug or "/.github/actions/" in slug:
            continue
        results.append((current_file, slug, ref, comment))
    return results


def _reusable_workflow_sha_refs_from_diff(
    diff: str,
) -> list[tuple[str, str, str, str]]:
    r"""Extract SHA-pinned refs from reusable-workflow ``uses:`` lines.

    ``_action_refs_from_diff()`` skips reusable-workflow lines (those
    whose slug contains ``.github/workflows/`` or ``.github/actions/``)
    because they are validated differently — tag refs like ``@main`` are
    valid for reusable workflows and do not need a SHA existence check.

    This function captures only 40-char hex SHA refs from those
    skipped lines so they can be validated via :func:`_verify_action_sha`.

    Returns ``[(file_path, slug, sha, comment), ...]`` — same shape as
    :func:`_action_refs_from_diff` so the caller can feed both into the
    same validation pipeline.

    >>> _reusable_workflow_sha_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\\n'
    ...     '+    uses: damien-robotsix/robotsix-github-workflows/'
    ...     '.github/workflows/docker-release.yml'
    ...     '@43309967ea8011400212a8995d33ca900ee2afed\\n'
    ... )
    [('.github/workflows/ci.yml', 'damien-robotsix/robotsix-github-workflows/.github/workflows/docker-release.yml', '43309967ea8011400212a8995d33ca900ee2afed', '')]

    >>> # Tag refs are skipped — valid for reusable workflows, no check needed.
    >>> _reusable_workflow_sha_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\\n'
    ...     '+    uses: my-org/my-repo/.github/workflows/ci.yml@main\\n'
    ... )
    []

    >>> # .github/actions/ is also matched.
    >>> _reusable_workflow_sha_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\\n'
    ...     '+    uses: my-org/my-repo/.github/actions/composite-action'
    ...     '@a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0\\n'
    ... )
    [('.github/workflows/ci.yml', 'my-org/my-repo/.github/actions/composite-action', 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0', '')]
    """
    results: list[tuple[str, str, str, str]] = []
    current_file: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/") and not line.startswith("+++ b/dev/null"):
            current_file = line[6:].strip()
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if current_file is None:
            continue
        m = _ACTION_USES_RE.search(line)
        if not m:
            continue
        slug, ref, comment = m.group(1), m.group(2), (m.group(3) or "").strip()
        # Only reusable-workflow / reusable-action refs.
        if "/.github/workflows/" not in slug and "/.github/actions/" not in slug:
            continue
        # Only 40-char hex SHAs — tag refs (@main, @v1) are valid and
        # do not need an existence check.
        if not _SHA_RE.match(ref):
            continue
        results.append((current_file, slug, ref, comment))
    return results


def _verify_action_sha(
    owner_repo: str, sha: str, token: str | None = None
) -> bool | None:
    """Best-effort verify *sha* exists in *owner_repo* via ``git ls-remote``.

    When *token* is provided, the URL is constructed via
    :func:`git_ops._authed_url` so ``git ls-remote`` can access private
    repos.  When *token* is ``None`` (e.g. test environment or no token
    configured), the public URL is used and private repos will return
    ``None`` (could not check) rather than a false-positive ``False``.

    Returns True when confirmed, False when the SHA is absent from
    ``ls-remote`` output, None when the check could not be performed
    (network error, timeout, non-zero exit, empty output, etc.).

    Note: ``git ls-remote`` patterns filter by *ref name*, not object
    SHA — so we call it without a pattern and grep the full output for
    the SHA.  Passing the SHA as a positional argument would filter
    refs by that hex string (always producing empty output).
    """
    # Defence-in-depth: owner_repo must match the expected format before
    # we construct a URL or pass anything to a subprocess.  A mismatch
    # means the caller extracted something unexpected from the diff;
    # bail out gracefully rather than proceeding.
    if not _OWNER_REPO_RE.match(owner_repo):
        return None  # Malformed owner/repo — cannot verify

    try:
        # Split and shlex-quote each component so CodeQL recognises the
        # sanitisation (the regex guard above is already sufficient, but
        # CodeQL's taint tracker does not model regex-based sanitizers).
        # shlex.quote is a no-op for [a-zA-Z0-9_.-] characters, so the
        # resulting URL is identical to the unsanitised version.
        owner, _, repo = owner_repo.partition("/")
        safe_owner = shlex.quote(owner)
        safe_repo = shlex.quote(repo)
        url = f"https://github.com/{safe_owner}/{safe_repo}.git"
        if token:
            url = git_ops._authed_url(url, token)
        result = subprocess.run(
            ["git", "ls-remote", url],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None  # Could not check
        if not result.stdout.strip():
            return None  # No output — could not verify (network filtered?)
        if sha not in result.stdout:
            return False  # SHA confirmed absent (ls-remote had other output)
        return True  # SHA confirmed
    except Exception:
        return None  # Any failure → skip existence check


def _validate_action_refs(
    action_refs: list[tuple[str, str, str, str]],
) -> list[dict[str, str]]:
    """Check each action ref for format validity (no-op: tag refs are OK).

    Third-party action refs (the only kind reaching this function —
    reusable-workflow refs are filtered by :func:`_action_refs_from_diff`)
    may use tag references (e.g. ``@v4``, ``@v5.4.0``).  Dependabot
    handles SHA pinning, so we no longer require a full 40-char hex
    commit SHA at the format level.

    Returns a list of violation dicts with keys: ``file``, ``slug``,
    ``ref``, ``comment``.  Currently always returns an empty list.

    The caller may still perform an optional existence check on refs that
    ARE 40-char hex SHAs via :func:`_verify_action_sha`.

    >>> _validate_action_refs([])
    []

    >>> _validate_action_refs([
    ...     ('.github/workflows/ci.yml', 'actions/checkout',
    ...      '11bd71901bbe5b1630ceea73d27597364c9af683', 'v4.2.2'),
    ... ])
    []

    >>> _validate_action_refs([
    ...     ('.github/workflows/ci.yml', 'actions/checkout', 'v4', ''),
    ... ])
    []
    """
    return []


def _load_file_map(ws) -> set[str] | None:
    """Read ``file_map.json`` from the ticket workspace, if any.

    Returns the set of in-scope paths. Returns ``None`` when the file
    is missing, empty, or unparseable — the review stage then treats
    every ask as in-scope (back-compat with tickets that have no
    file map yet, e.g. legacy or scope-free flows).
    """
    p = ws.artifacts_dir / "file_map.json"
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, list) or not raw:
        return None
    paths = {
        entry["file"] for entry in raw if isinstance(entry, dict) and "file" in entry
    }
    return paths or None


def _file_in_scope(ask_file: str, file_map: set[str]) -> bool:
    """True when *ask_file* refers to the same path as some file_map entry.

    Tolerant of refine vs review path-format mismatches: refine often
    writes short suffixes (``static/board.js``) into file_map while the
    review agent uses canonical repo-relative paths
    (``src/robotsix_mill/runtime/static/board.js``). Exact string
    comparison would mis-classify the latter as out-of-scope and force
    every review ask on that file into a dependency ticket — which is
    how the board ended up with a pile of spurious review-source drafts.

    Match rule:
      1. exact equality, or
      2. one path is a path-suffix of the other (sharing a ``/`` boundary).
    """
    if ask_file in file_map:
        return True
    for m in file_map:
        if ask_file.endswith("/" + m) or m.endswith("/" + ask_file):
            return True
    return False


def _split_asks(
    asks: list[ReviewAsk],
    file_map: set[str] | None,
) -> tuple[list[ReviewAsk], list[ReviewAsk]]:
    """Partition ``asks`` into ``(in_scope, out_of_scope)``.

    An ask is out-of-scope when it touches at least one file NOT in
    ``file_map`` (under :func:`_file_in_scope` semantics — path-suffix
    tolerant, so a refine-side ``static/board.js`` matches a review-side
    ``src/robotsix_mill/runtime/static/board.js``). Asks with empty
    ``files_touched`` are treated as in-scope (file-less clarifications
    stay with the parent). When ``file_map`` is None (legacy /
    scope-free), every ask is in-scope.
    """
    if file_map is None:
        return list(asks), []
    in_scope: list[ReviewAsk] = []
    out_of_scope: list[ReviewAsk] = []
    for ask in asks:
        if not ask.files_touched:
            in_scope.append(ask)
            continue
        if any(not _file_in_scope(f, file_map) for f in ask.files_touched):
            out_of_scope.append(ask)
        else:
            in_scope.append(ask)
    return in_scope, out_of_scope


def _spawn_dependency_tickets(
    parent: Ticket,
    asks: list[ReviewAsk],
    ctx,
) -> list[str]:
    """Materialise each out-of-scope ask as a fresh ticket on the same
    board, return their IDs.

    Title is a short paraphrase of the ask description; body captures
    the full description plus the files the ask would touch so the
    refine agent has enough context to produce a real spec. ``source``
    is ``"review"`` so the operator can trace these back to the
    review pass that spawned them.
    """
    ids: list[str] = []
    for ask in asks:
        title = (
            ask.title.strip() or ask.description.splitlines()[0] or "review follow-up"
        )[:120]
        body_lines = [ask.description.strip()]
        if ask.files_touched:
            body_lines.append("")
            body_lines.append("Files involved:")
            body_lines.extend(f"- `{f}`" for f in ask.files_touched)
        body_lines.append("")
        body_lines.append(
            f"(Spawned by review on parent ticket `{parent.id}` — its "
            "scope did not cover these files.)"
        )
        child = ctx.service.create(
            title,
            "\n".join(body_lines),
            source="review",
            board_id=parent.board_id or None,
            priority=parent.priority,
        )
        ids.append(child.id)
    return ids


def _gaps_already_addressed(
    asks: list[ReviewAsk],
    modified_paths: list[str],
) -> tuple[list[ReviewAsk], list[ReviewAsk]]:
    """Partition *asks* into *(already_addressed, still_pending)*.

    An ask is "already addressed" when every file it would touch already
    appears in *modified_paths* — the implementer's branch diff includes
    changes to those files, so the gap flagged by the reviewer may have
    been handled inline.  Asks with empty ``files_touched`` are treated as
    still pending (we cannot verify them from the diff alone).
    """
    mp_set = set(modified_paths)
    already: list[ReviewAsk] = []
    pending: list[ReviewAsk] = []
    for ask in asks:
        if not ask.files_touched:
            pending.append(ask)
        elif all(f in mp_set for f in ask.files_touched):
            already.append(ask)
        else:
            pending.append(ask)
    return already, pending


def _build_prior_context(ticket, ctx, ws) -> str | None:
    """Assemble prior review comments and the implement agent's rebuttal
    from the last round into a ``prior-context`` fenced block.

    Returns ``None`` when neither source has content (first review round).
    """
    from ..agents.prompt_blocks import section
    from ..core.text_utils import tail_keep

    parts: list[str] = []

    # Bound each prior-context component independently with a tail-keep
    # (most-recent content survives) so multi-round reviews don't re-pay
    # for the entire accumulated comment history + full rebuttal each
    # round. Apply per-component (not to the combined block) so we never
    # cut through a ``section`` fence marker. 0 = no cap.
    max_chars = ctx.settings.review_prior_context_max_chars

    def _cap(text: str, label: str) -> str:
        if max_chars and len(text) > max_chars:
            return tail_keep(text, max_chars, label=label)
        return text

    prior_comments = ctx.service.list_comments(ticket.id)
    if prior_comments:
        # Closed threads are normally skipped (resolved REQUEST_CHANGES
        # feedback the implement agent already addressed). EXCEPTION: a
        # closed top-level [ASK_USER] thread carries the operator's
        # decision from a NEEDS_DISCUSSION pause — the re-run review
        # MUST see it (and its replies) or it will just re-ask the same
        # question and loop. Keep those; drop everything else closed.
        askuser_ids = {
            c.id
            for c in prior_comments
            if c.parent_id is None and c.body.startswith("[ASK_USER]")
        }
        excluded_ids = {
            c.id
            for c in prior_comments
            if c.closed_at is not None and c.id not in askuser_ids
        }
        formatted = "\n".join(
            f"{'  ↳ ' if c.parent_id is not None else ''}[{c.author}] {c.body}"
            for c in prior_comments
            if c.id not in excluded_ids and c.parent_id not in excluded_ids
        )
        if formatted:
            parts.append(
                section(
                    "prior-review-comments",
                    _cap(formatted, "prior-review-comments"),
                )
            )

    implement_md = ws.artifacts_dir / "implement.md"
    if implement_md.exists():
        parts.append(
            section(
                "implement-rebuttal",
                _cap(
                    implement_md.read_text(encoding="utf-8"),
                    "implement-rebuttal",
                ),
            )
        )

    if not parts:
        return None
    return section("prior-context", "\n\n".join(parts))


def _maybe_cache(ws: Workspace, input_hash: str | None, outcome: Outcome) -> None:
    """Persist *outcome* to the stage-outcome cache, except for
    ``AWAITING_USER_REPLY`` outcomes.

    NEEDS_DISCUSSION verdicts produce AWAITING_USER_REPLY — caching
    them would create an operator-answer loop because the cache key
    (spec+diff) doesn't include the operator's response.  When the
    ticket resumes after the operator replies, the unchanged spec+diff
    would hit the cache and re-ask the same question.
    """
    if input_hash and outcome.next_state != State.AWAITING_USER_REPLY:
        from ._stage_cache import _update

        _update(ws, "review", input_hash, outcome)


class _DiffMeta:
    """Resolved diff and metadata bundle returned by
    :meth:`ReviewStage._resolve_diff_and_metadata`.

    Carries the bounded diff, HEAD SHA, input hash, repo directory,
    extracted paths/refs, and an optional GitHub token — everything
    the orchestrator needs downstream without threading a dozen local
    variables.
    """

    __slots__ = (
        "action_refs",
        "diff",
        "gh_token",
        "head_sha",
        "input_hash",
        "modified_paths",
        "repo_dir",
        "reusable_workflow_refs",
        "workflow_refs",
    )

    def __init__(
        self,
        diff: str,
        head_sha: str,
        input_hash: str,
        repo_dir: Path,
        modified_paths: list[str],
        workflow_refs: set[str],
        action_refs: list[tuple[str, str, str, str]],
        reusable_workflow_refs: list[tuple[str, str, str, str]],
        gh_token: str | None,
    ) -> None:
        self.diff = diff
        self.head_sha = head_sha
        self.input_hash = input_hash
        self.repo_dir = repo_dir
        self.modified_paths = modified_paths
        self.workflow_refs = workflow_refs
        self.action_refs = action_refs
        self.reusable_workflow_refs = reusable_workflow_refs
        self.gh_token = gh_token


class ReviewStage(Stage):
    """Check out the target branch and perform automated code review on the ticket's implemented changes."""

    name = "review"
    input_state = State.CODE_REVIEW
    traced = True

    # ── orchestrator ────────────────────────────────────────────────
    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process a CODE_REVIEW ticket: refresh the clone, check out the
        ticket branch, and run the automated reviewer agent against the
        diff."""
        s = ctx.settings
        ws = ctx.service.workspace(ticket)

        repos = implemented_repos(ws, s, ticket)
        if not repos:
            return Outcome(
                State.BLOCKED,
                "no repository clone to review (re-run implement)",
            )

        target_branch = target_branch_for(s, ctx.repo_config)

        # 1. Resolve diff + metadata; short-circuit on empty diff / cache hit.
        dm = self._resolve_diff_and_metadata(ws, s, ctx, repos, target_branch, ticket)
        if isinstance(dm, Outcome):
            return dm

        spec = ws.read_description()
        prior_context = _build_prior_context(ticket, ctx, ws)

        board_png = ws.artifacts_dir / "board.png"
        screenshot_path = board_png if board_png.exists() else None

        # 2. Cross-repo reusable-workflow clones.
        extra_roots = self._clone_cross_repo_workflows(
            ws, s, ctx, dm.workflow_refs, ticket
        )

        # 3. Model-level routing for cheap changes.
        level = self._resolve_review_level(dm.repo_dir, target_branch)

        # 4. Run the blind review agent.
        try:
            verdict: ReviewVerdict = run_review_agent(
                settings=s,
                diff=dm.diff,
                spec=spec,
                level=level,
                prior_context=prior_context,
                repo_dir=dm.repo_dir,
                reference_files=dm.modified_paths,
                screenshot_path=screenshot_path,
                extra_roots=extra_roots,
            )
        except Exception as e:
            log.exception("%s: review agent error", ticket.id)
            # Transient model blips (OpenRouter 5xx/429/timeout, the
            # DeepSeek reasoning-400) should get a fresh stage re-run via
            # the worker's stage-retry rather than a hard BLOCK needing a
            # manual resume — same fix as implement.py.
            from ..runtime.transient_errors import (
                is_insufficient_credit,
                parse_credit_shortfall,
                reraise_if_transient,
            )

            if is_insufficient_credit(e):
                from ..runtime.credit_status import record_low_credit

                detail = parse_credit_shortfall(e)
                record_low_credit(detail=detail)

            reraise_if_transient(e)
            return Outcome(
                State.BLOCKED,
                f"review agent error — resumable: {e}",
            )

        # 5. Action-ref SHA validation (mutates verdict on violations).
        verdict = self._validate_action_shas(
            dm.action_refs, dm.reusable_workflow_refs, dm.gh_token, verdict
        )

        # 6. Persist review artifact.
        ws.artifacts_dir.joinpath("review.md").write_text(
            f"verdict: {verdict.verdict}\n"
            f"auto_merge_eligible: {str(verdict.auto_merge_eligible).lower()}\n"
            f"head_sha: {dm.head_sha}\n"
            f"board_screenshot: {'present' if screenshot_path else 'absent'}\n"
            f"comment: {_collapse_comments(verdict.comments)}\n",
            encoding="utf-8",
        )

        # 7. Route verdict to the next stage.
        return self._handle_review_verdict(
            verdict,
            ticket,
            ctx,
            ws,
            s,
            dm.input_hash,
            dm.modified_paths,
            dm.repo_dir,
        )

    # ── private helpers ─────────────────────────────────────────────

    def _resolve_diff_and_metadata(
        self,
        ws: Workspace,
        s: Settings,
        ctx: StageContext,
        repos: list[ImplementedRepo],
        target_branch: str | None,
        ticket: Ticket,
    ) -> "_DiffMeta | Outcome":
        """Compute the combined diff, extract metadata, and handle early
        returns (empty diff, stage-outcome cache hit).

        Returns a :class:`_DiffMeta` bundle on success, or an
        :class:`Outcome` that *run* should return immediately.
        """
        # Compute the combined diff across every implemented clone. Each
        # repo is fetched with a freshly-minted token for ITS forge (the
        # baked-in clone token expires ~1h after clone, so a stale origin
        # URL would 401 on the fetch). For >1 repo, prefix each repo's
        # diff with a header so the reviewer can tell them apart.
        try:
            diff = combined_diff(s, ctx.repo_config, repos, target_branch or "")
        except Exception as e:
            from ..runtime.transient_errors import reraise_if_transient
            from ..vcs.git_ops import redact_credentials

            reraise_if_transient(e)
            # str(CalledProcessError) reprs the full argv — including
            # the tokenized fetch URL. Redact before it hits the note.
            return Outcome(
                State.BLOCKED,
                f"failed to compute diff: {redact_credentials(str(e))}",
            )

        # The review agent's file tools are rooted at the first clone;
        # for multi-repo the per-file pre-seed (below) carries the rest.
        repo_dir = repos[0].repo_dir

        # Empty diff → no-op implementation, approve so deliver can handle it.
        if not diff.strip():
            log.info("%s: empty diff — approving without review", ticket.id)
            return Outcome(State.DOCUMENTING, "empty diff (no-op implementation)")

        # Snapshot the branch-tip HEAD SHA so downstream consumers
        # (stage cache, auto-merge eligibility) can detect when a later
        # rebase or force-push has made this review stale.
        head_sha = git_ops.head_sha(Path(repo_dir))

        # --- stage-outcome cache: short-circuit when input is unchanged ---
        from ._stage_cache import _check, review_input_hash

        input_hash = review_input_hash(ws, diff, head_sha)
        cached = _check(ws, ReviewStage.name, input_hash)
        if cached is not None:
            log.info(
                "%s: review cache hit (hash=%s…) → %s",
                ticket.id,
                input_hash[:12],
                cached.next_state.value,
            )
            return cached

        # Derive modified paths, workflow refs, AND action refs from the
        # UNTRUNCATED diff so middle truncation (below) never drops a
        # ``+++ b/<path>`` header or a ``uses:`` line and silently shrinks
        # the preseed file set, the cross-repo clone set, or the action-ref
        # validation. The agent receives the bounded diff; the preseed and
        # extra_roots still cover every referenced file and repo.
        modified_paths = git_ops._paths_from_diff(diff)
        workflow_refs = _workflow_refs_from_diff(diff)
        action_refs = _action_refs_from_diff(diff)
        reusable_workflow_refs = _reusable_workflow_sha_refs_from_diff(diff)

        # Fetch a GitHub token for authenticated SHA verification against
        # private repos.  When no token is configured (e.g. test
        # environments), ``gh_token`` stays ``None`` and
        # ``_verify_action_sha`` uses the public URL — which degrades
        # gracefully (returns ``None`` = could not check).
        try:
            gh_token = github_token(s, ctx.repo_config)
        except Exception:
            gh_token = None

        # Bound the combined diff before it reaches the review prompt. The
        # raw ``git diff origin/<target>...HEAD`` can balloon to megabytes
        # (divergent base, generated/lockfile churn, accumulated branch
        # history) regardless of how few lines the intended change touches,
        # overflowing even a 1M-token model context. Middle-truncate so both
        # early and late files keep representation. 0 disables the cap.
        from ..core.text_utils import head_tail_keep

        diff = head_tail_keep(diff, s.review_diff_max_chars, label="git-diff")

        return _DiffMeta(
            diff=diff,
            head_sha=head_sha,
            input_hash=input_hash,
            repo_dir=repo_dir,
            modified_paths=modified_paths,
            workflow_refs=workflow_refs,
            action_refs=action_refs,
            reusable_workflow_refs=reusable_workflow_refs,
            gh_token=gh_token,
        )

    def _clone_cross_repo_workflows(
        self,
        ws: Workspace,
        s: Settings,
        ctx: StageContext,
        workflow_refs: set[str],
        ticket: Ticket,
    ) -> list[Path] | None:
        """Clone sibling repos referenced by reusable-workflow ``uses:``
        lines so the review agent can inspect their interface.

        Returns a list of :class:`~pathlib.Path` entries (``extra_roots``)
        or ``None`` when no cross-repo clones were needed.
        """
        if not workflow_refs:
            return None

        # Exclude the current repo — the agent already has repo_dir.
        current_remote = (
            ctx.repo_config.forge_remote_url if ctx.repo_config else None
        ) or s.forge_remote_url
        if current_remote:
            try:
                current_owner, current_repo = _parse_owner_repo(current_remote)
                current_slug = f"{current_owner}/{current_repo}"
                workflow_refs.discard(current_slug)
            except Exception:
                log.debug(
                    "%s: cannot parse current repo remote, skipping exclusion",
                    ticket.id,
                )

        if not workflow_refs:
            return None

        clone_roots: list[Path] = []

        # Resolve refs via repos config: for each referenced
        # owner/repo, pick up an existing clone (meta-layout
        # or prior .review-roots clone) or clone fresh.
        try:
            all_repos = get_repos_config().repos
        except Exception:
            all_repos = {}

        for repo_id, rc in all_repos.items():
            remote = rc.forge_remote_url
            if not remote:
                continue
            try:
                owner, repo = _parse_owner_repo(remote)
            except Exception:
                log.debug(
                    "%s: cannot parse remote %s, skipping",
                    ticket.id,
                    remote,
                )
                continue
            slug = f"{owner}/{repo}"
            if slug not in workflow_refs:
                continue

            # 1) Already cloned in .review-roots (prior pass)
            dest = ws.dir / ".review-roots" / repo_id
            if dest.is_dir():
                clone_roots.append(dest)
                continue

            # 2) Already cloned in meta-layout
            meta_dest = ws.dir / "repos" / repo_id
            if meta_dest.is_dir():
                clone_roots.append(meta_dest)
                continue

            # 3) Clone fresh, respecting per-repo branch override
            try:
                token = github_token(s, repo_config=rc)
                branch = target_branch_for(s, rc)
                git_ops.clone(remote, dest, branch, token)
                clone_roots.append(dest)
            except Exception:
                log.warning(
                    "%s: failed to clone %s for cross-repo review",
                    ticket.id,
                    slug,
                )

        return clone_roots or None

    @staticmethod
    def _resolve_review_level(
        repo_dir: Path,
        target_branch: str | None,
    ) -> int | None:
        """Return 1 for cheap (config-only / rename-only / small-refactor)
        changes so the review runs on the level-1 model; ``None`` (use
        default level) otherwise.  Fail-closed: any git error returns
        ``None``.
        """
        if target_branch is None:
            return None
        config_only: bool = _is_config_only_change(repo_dir, target_branch)
        rename_only: bool = _is_rename_only_change(repo_dir, target_branch)
        small_refactor: bool = _is_small_mechanical_refactor(repo_dir, target_branch)
        if config_only or rename_only or small_refactor:
            return 1
        return None

    @staticmethod
    def _validate_action_shas(
        action_refs: list[tuple[str, str, str, str]],
        reusable_workflow_refs: list[tuple[str, str, str, str]],
        gh_token: str | None,
        verdict: "ReviewVerdict",
    ) -> "ReviewVerdict":
        """Validate 40-char hex SHA refs in *action_refs* and
        *reusable_workflow_refs* via ``git ls-remote``, injecting any
        missing-SHA violations as synthetic REQUEST_CHANGES.

        Returns *verdict* (mutated in-place when violations are found).
        """
        action_violations = _validate_action_refs(action_refs)

        # Optional best-effort existence check for SHA refs:
        # for each ref that IS a 40-char hex SHA, confirm it exists via
        # ``git ls-remote``.  Any failure (network error, timeout,
        # non-zero exit) degrades gracefully — the SHA is not flagged.
        # Tag references (e.g. @v4) are skipped here.
        for file_path, slug, ref, comment in action_refs:
            if _SHA_RE.match(ref):
                parts = slug.split("/")
                if len(parts) >= 2:
                    owner_repo = f"{parts[0]}/{parts[1]}"
                    exists = _verify_action_sha(owner_repo, ref, gh_token)
                    if exists is False:
                        action_violations.append(
                            {
                                "file": file_path,
                                "slug": slug,
                                "ref": ref,
                                "comment": comment,
                            }
                        )

        # Validate SHA refs from reusable-workflow ``uses:`` lines
        # (previously skipped by ``_action_refs_from_diff``).
        for file_path, slug, ref, comment in reusable_workflow_refs:
            parts = slug.split("/")
            if len(parts) >= 2:
                owner_repo = f"{parts[0]}/{parts[1]}"
                exists = _verify_action_sha(owner_repo, ref, gh_token)
                if exists is False:
                    action_violations.append(
                        {
                            "file": file_path,
                            "slug": slug,
                            "ref": ref,
                            "comment": comment,
                        }
                    )

        if action_violations:
            synthetic_asks: list[ReviewAsk] = []
            for v in action_violations:
                comment_part = f" # {v['comment']}" if v["comment"] else ""
                title = (f"Verify commit SHA for {v['slug']} in {v['file']}")[:80]
                description = (
                    f"Commit SHA `{v['ref']}` for action "
                    f"`{v['slug']}{comment_part}` in `{v['file']}` "
                    f"was not found in the upstream repository. "
                    f"Verify the SHA is correct or replace it with a "
                    f"valid 40-char commit SHA."
                )
                synthetic_asks.append(
                    ReviewAsk(
                        title=title,
                        description=description,
                        files_touched=[v["file"]],
                    )
                )

            # Force REQUEST_CHANGES regardless of LLM verdict.
            # Non-existent commit SHAs are a hard correctness issue.
            verdict.verdict = "REQUEST_CHANGES"
            verdict.auto_merge_eligible = False
            verdict.request_changes = synthetic_asks + list(verdict.request_changes)
            if verdict.comments:
                verdict.comments = (
                    "Action ref validation failed: commit SHA not found "
                    "in upstream repo (see request_changes entries "
                    "below).\n\n" + verdict.comments
                )
            else:
                verdict.comments = (
                    "Action ref validation failed: commit SHA not found "
                    "in upstream repo (see request_changes entries "
                    "below)."
                )

        return verdict

    def _handle_review_verdict(
        self,
        verdict: "ReviewVerdict",
        ticket: Ticket,
        ctx: StageContext,
        ws: Workspace,
        s: Settings,
        input_hash: str,
        modified_paths: list[str],
        repo_dir: Path,
    ) -> Outcome:
        """Route *verdict* to the next pipeline state.

        APPROVE → DOCUMENTING; REQUEST_CHANGES → READY (or BLOCKED on
        convergence / round-cap exhaustion); NEEDS_DISCUSSION →
        AWAITING_USER_REPLY.
        """
        if verdict.verdict == "APPROVE":
            ctx.service.set_review_rounds(ticket.id, 0)
            outcome = Outcome(State.DOCUMENTING, "review approved")
            _maybe_cache(ws, input_hash, outcome)
            return outcome

        if verdict.verdict == "REQUEST_CHANGES":
            return self._handle_request_changes(
                verdict, ticket, ctx, ws, s, input_hash, modified_paths, repo_dir
            )

        # NEEDS_DISCUSSION — genuine human-decision verdict.
        # Post as [ASK_USER] and pause; operator reply auto-resumes.
        ctx.service.add_comment(
            ticket.id,
            f"[ASK_USER]\n\n{verdict.comments}",
            author="review",
        )
        outcome = Outcome(State.AWAITING_USER_REPLY, verdict.comments)
        _maybe_cache(ws, input_hash, outcome)
        return outcome

    def _handle_request_changes(
        self,
        verdict: "ReviewVerdict",
        ticket: Ticket,
        ctx: StageContext,
        ws: Workspace,
        s: Settings,
        input_hash: str,
        modified_paths: list[str],
        repo_dir: Path,
    ) -> Outcome:
        """Process a REQUEST_CHANGES verdict: round tracking, convergence
        detection, ask splitting, and follow-up spawning."""
        rounds = ticket.review_rounds + 1
        ctx.service.set_review_rounds(ticket.id, rounds)

        # Round-cap exhaustion.
        if rounds >= s.review_max_rounds:
            ctx.service.add_comment(
                ticket.id,
                f"Review round cap exhausted ({rounds}/{s.review_max_rounds} "
                f"REQUEST_CHANGES rounds). Escalating to DELIVERABLE for "
                f"human merge approval.\n\nLast review verdict:\n{verdict.comments}",
                author="review",
            )
            ctx.service.set_review_rounds(ticket.id, 0)
            outcome = Outcome(
                State.DOCUMENTING,
                f"review rounds exhausted ({rounds}/{s.review_max_rounds})",
            )
            _maybe_cache(ws, input_hash, outcome)
            return outcome

        # Convergence detection: repeated findings fingerprint.
        import hashlib

        fp = hashlib.sha256()
        for ask in sorted(verdict.request_changes, key=lambda a: a.title or ""):
            fp.update((ask.title or "").encode())
            fp.update((ask.description or "").encode())
            for f in sorted(ask.files_touched or []):
                fp.update(f.encode())
        fingerprint = fp.hexdigest()
        fp_path = ws.artifacts_dir / "findings_fingerprint.txt"
        prev_fp = None
        if fp_path.exists():
            try:
                prev_fp = fp_path.read_text(encoding="utf-8").strip()
            except OSError:
                log.warning("%s: failed to read findings fingerprint", ticket.id)
        if prev_fp == fingerprint:
            ctx.service.add_comment(
                ticket.id,
                f"Convergence detected: review round {rounds} found the "
                f"same {len(verdict.request_changes)} issue(s) as the "
                "previous round. Implement is not making progress on "
                "these findings — escalating to BLOCKED for human "
                "inspection.",
                author="review",
            )
            ctx.service.set_review_rounds(ticket.id, 0)
            outcome = Outcome(
                State.BLOCKED,
                "convergence: repeated review findings — implement stuck",
            )
            _maybe_cache(ws, input_hash, outcome)
            return outcome
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        fp_path.write_text(fingerprint, encoding="utf-8")

        # Split asks against the ticket's file_map.
        file_map = _load_file_map(ws)
        in_scope, out_of_scope = _split_asks(verdict.request_changes, file_map)

        already_addressed: list[ReviewAsk] = []
        still_out_of_scope: list[ReviewAsk] = []
        if out_of_scope:
            already_addressed, still_out_of_scope = _gaps_already_addressed(
                out_of_scope, modified_paths
            )

        # Verify any PR/commit claims in the "already addressed" asks.
        if already_addressed:
            truly_addressed: list[ReviewAsk] = []
            for ask in already_addressed:
                if (
                    ask.files_touched
                    and ask.description
                    and not verify_claim(ask.description, ask.files_touched, repo_dir)
                ):
                    log.info(
                        "%s: review ask claim unverified — "
                        "cited refs in '%s' do not touch %s; "
                        "treating as still out-of-scope",
                        ticket.id,
                        ask.description[:120],
                        ", ".join(ask.files_touched[:5]),
                    )
                    still_out_of_scope.append(ask)
                else:
                    truly_addressed.append(ask)
            already_addressed = truly_addressed

        if already_addressed:
            lines = [
                f"Review found {len(already_addressed)} gap(s) that appear "
                "already addressed in the implementer's commits — "
                "no follow-up needed:",
                "",
            ]
            for a in already_addressed:
                desc = a.description.splitlines()[0][:120]
                lines.append(f"- {desc}")
            ctx.service.add_comment(ticket.id, "\n".join(lines), author="review")

        if still_out_of_scope:
            new_ids = _spawn_dependency_tickets(ticket, still_out_of_scope, ctx)
            for nid in new_ids:
                ctx.service.set_depends_on(nid, [ticket.id])
            lines = [
                f"Review found {len(still_out_of_scope)} out-of-scope "
                "ask(s) — spawned as follow-up ticket(s) that depend on "
                "this one (they run after it merges):",
                "",
            ]
            for nid, ask in zip(new_ids, still_out_of_scope, strict=True):
                desc = ask.description.splitlines()[0][:120]
                lines.append(f"- `{nid}` — {desc}")
            ctx.service.add_comment(ticket.id, "\n".join(lines), author="review")

        if in_scope:
            # In-scope changes remain — re-implement just those.
            body = _sanitize_comments(verdict.comments)
            if still_out_of_scope:
                body = (
                    _sanitize_comments(verdict.comments)
                    + "\n\nIn-scope items to fix now (out-of-scope asks were "
                    "spawned as follow-ups):\n"
                    + "\n".join(
                        f"- {a.description.splitlines()[0][:200]}" for a in in_scope
                    )
                )
            ctx.service.add_comment(ticket.id, body, author="review")
            outcome = Outcome(State.READY, verdict.comments)
            _maybe_cache(ws, input_hash, outcome)
            return outcome

        if still_out_of_scope:
            # No in-scope changes: approve so follow-ups can run after merge.
            ctx.service.set_review_rounds(ticket.id, 0)
            outcome = Outcome(
                State.DOCUMENTING,
                f"approved; {len(still_out_of_scope)} out-of-scope "
                "ask(s) spawned as follow-ups",
            )
            _maybe_cache(ws, input_hash, outcome)
            return outcome

        if already_addressed:
            # Every out-of-scope ask was already addressed — approve.
            ctx.service.set_review_rounds(ticket.id, 0)
            outcome = Outcome(
                State.DOCUMENTING,
                f"approved; {len(already_addressed)} review gap(s) "
                "already addressed in the implementer's commits",
            )
            _maybe_cache(ws, input_hash, outcome)
            return outcome

        # REQUEST_CHANGES with no actionable asks — re-implement against
        # the narrative comments.
        ctx.service.add_comment(
            ticket.id, _sanitize_comments(verdict.comments), author="review"
        )
        outcome = Outcome(State.READY, verdict.comments)
        _maybe_cache(ws, input_hash, outcome)
        return outcome
