"""Stateless review helpers — extracted from review.py.

These 16 pure helper functions are stateless and operate solely on their
arguments.  The :class:`_DiffMeta` dataclass and :class:`ReviewStage`
orchestrator remain in :mod:`robotsix_mill.stages.review`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import subprocess
from pathlib import Path

from ..agents.reviewing import ReviewAsk, ReviewVerdict
from ..core.models import Comment, Ticket
from ..core.states import ASK_USER_MARKER, State
from ..core.workspace import Workspace
from ..vcs import git_ops
from .base import Outcome, StageContext
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
    ...     '+++ b/.github/workflows/ci.yml\n'
    ...     '+    uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2\n'
    ... )
    [('.github/workflows/ci.yml', 'actions/checkout', '11bd71901bbe5b1630ceea73d27597364c9af683', 'v4.2.2')]

    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\n'
    ...     '+    uses: actions/checkout@v4\n'
    ... )
    [('.github/workflows/ci.yml', 'actions/checkout', 'v4', '')]

    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\n'
    ...     '+    uses: github/codeql-action/init@6b0550b4a2a7c00e939e5501b0c0b3f654b3d8e4 # v3.29.2\n'
    ... )
    [('.github/workflows/ci.yml', 'github/codeql-action/init', '6b0550b4a2a7c00e939e5501b0c0b3f654b3d8e4', 'v3.29.2')]

    >>> # Local refs are skipped.
    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\n'
    ...     '+    uses: ./.github/actions/my-action@main\n'
    ... )
    []

    >>> # Docker refs are skipped.
    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\n'
    ...     '+    uses: docker://ubuntu:latest\n'
    ... )
    []

    >>> # Reusable-workflow refs are skipped (already handled by _WORKFLOW_RE).
    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\n'
    ...     '+    uses: my-org/my-repo/.github/workflows/ci.yml@v1\n'
    ... )
    []

    >>> # +++ header lines are not scanned.
    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\n'
    ... )
    []

    >>> # Deleted lines (^-prefixed) are not scanned.
    >>> _action_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\n'
    ...     '-    uses: evilcorp/backdoor@v1\n'
    ...     '+    uses: actions/checkout@v4\n'
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
    ...     '+++ b/.github/workflows/ci.yml\n'
    ...     '+    uses: damien-robotsix/robotsix-github-workflows/'
    ...     '.github/workflows/docker-release.yml'
    ...     '@43309967ea8011400212a8995d33ca900ee2afed\n'
    ... )
    [('.github/workflows/ci.yml', 'damien-robotsix/robotsix-github-workflows/.github/workflows/docker-release.yml', '43309967ea8011400212a8995d33ca900ee2afed', '')]

    >>> # Tag refs are skipped — valid for reusable workflows, no check needed.
    >>> _reusable_workflow_sha_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\n'
    ...     '+    uses: my-org/my-repo/.github/workflows/ci.yml@main\n'
    ... )
    []

    >>> # .github/actions/ is also matched.
    >>> _reusable_workflow_sha_refs_from_diff(
    ...     '+++ b/.github/workflows/ci.yml\n'
    ...     '+    uses: my-org/my-repo/.github/actions/composite-action'
    ...     '@a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0\n'
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


def _round_cap_directives(
    verdict: ReviewVerdict,
    ticket: Ticket,
    ctx: StageContext,
) -> tuple[list[ReviewAsk], list[str]]:
    """Collect the concrete review/ask_user directives still outstanding
    when the REQUEST_CHANGES round cap is exhausted.

    Returns ``(review_asks, ask_user_directives)``:

    * *review_asks* — the current verdict's ``request_changes`` entries
      that carry a concrete title or description (a real, actionable ask,
      not an empty placeholder). A REQUEST_CHANGES verdict that still
      carries such asks means the reviewer's explicit asks were never
      satisfied.
    * *ask_user_directives* — one entry per ``[ASK_USER]`` thread that has
      at least one operator reply: the operator's answer (their
      directive), prefixed with the question it answers.

    The review stage uses these on round-cap exhaustion: instead of
    auto-escalating to delivery with explicit directives possibly
    unimplemented (which historically merged work whose reviewer/operator
    asks were missing — e.g. PR #3087), it pauses on a human
    "directives satisfied?" gate listing them.
    """
    review_asks = [
        a
        for a in verdict.request_changes
        if (a.title or "").strip() or (a.description or "").strip()
    ]

    directive_lines: list[str] = []
    # ``list(...)`` re-boxes the SQLModel-plugin ``list?[Comment]`` return
    # into a plain ``list[Comment]`` so mypy --strict treats it as iterable.
    comments: list[Comment] = list(ctx.service.list_comments(ticket.id))
    ask_thread_ids = {
        c.id
        for c in comments
        if c.parent_id is None and (c.body or "").startswith(ASK_USER_MARKER)
    }
    for thread in (c for c in comments if c.id in ask_thread_ids):
        replies = [
            c for c in comments if c.parent_id == thread.id and (c.body or "").strip()
        ]
        if not replies:
            continue  # unanswered question — nothing the operator directed
        question = _collapse_comments(thread.body)
        answers = "; ".join(_collapse_comments(c.body) for c in replies)
        directive_lines.append(f"{question}  →  {answers}")
    return review_asks, directive_lines


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


def _detect_convergence(
    verdict: ReviewVerdict,
    ticket_id: str,
    rounds: int,
    ws: Workspace,
    ctx: StageContext,
    input_hash: str,
) -> Outcome | None:
    """Detect repeated review findings across rounds via SHA-256 fingerprint.

    Returns an ``Outcome(BLOCKED, …)`` when the current round's findings
    match the previous round's exactly — the implement agent is stuck.
    Returns ``None`` when no convergence is detected (the caller proceeds
    normally).  Writes the current fingerprint to disk so the next round
    can compare against it.
    """
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
            log.warning("%s: failed to read findings fingerprint", ticket_id)
    if prev_fp == fingerprint:
        ctx.service.add_comment(
            ticket_id,
            f"Convergence detected: review round {rounds} found the "
            f"same {len(verdict.request_changes)} issue(s) as the "
            "previous round. Implement is not making progress on "
            "these findings — escalating to BLOCKED for human "
            "inspection.",
            author="review",
        )
        ctx.service.set_review_rounds(ticket_id, 0)
        outcome = Outcome(
            State.BLOCKED,
            "convergence: repeated review findings — implement stuck",
        )
        _maybe_cache(ws, input_hash, outcome)
        return outcome
    fp_path.parent.mkdir(parents=True, exist_ok=True)
    fp_path.write_text(fingerprint, encoding="utf-8")
    return None


def _verify_already_addressed_asks(
    already_addressed: list[ReviewAsk],
    ticket_id: str,
    repo_dir: Path,
) -> tuple[list[ReviewAsk], list[ReviewAsk]]:
    """Verify claims in *already_addressed* asks against the repo.

    Returns ``(truly_addressed, unverified)``.  An ask whose
    :func:`verify_claim` fails is moved to *unverified* so the caller
    can treat it as still-out-of-scope.  Asks with no files_touched or
    no description are treated as verified (we cannot disprove them).
    """
    if not already_addressed:
        return [], []
    truly: list[ReviewAsk] = []
    unverified: list[ReviewAsk] = []
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
                ticket_id,
                ask.description[:120],
                ", ".join(ask.files_touched[:5]),
            )
            unverified.append(ask)
        else:
            truly.append(ask)
    return truly, unverified
