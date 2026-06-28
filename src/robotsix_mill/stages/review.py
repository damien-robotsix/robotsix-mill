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
from ..core.models import Ticket
from ..core.states import State
from ..forge.auth import github_token
from ..forge.github import _parse_owner_repo
from ..vcs import git_ops
from ._implemented_repos import combined_diff, implemented_repos
from .base import Outcome, Stage, StageContext

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
    """Extract action ``uses:`` references from added diff lines.

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


def _verify_action_sha(owner_repo: str, sha: str) -> bool | None:
    """Best-effort verify *sha* exists in *owner_repo* via ``git ls-remote``.

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
        result = subprocess.run(  # noqa: S603
            ["git", "ls-remote", url],  # noqa: S607
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
    """Check each action ref for a valid 40-char hex SHA (format-only).

    Returns a list of violation dicts with keys: ``file``, ``slug``,
    ``ref``, ``comment``.  Does NOT perform an existence check — that is
    done separately (and optionally) by the caller via
    :func:`_verify_action_sha`.

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
    [{'file': '.github/workflows/ci.yml', 'slug': 'actions/checkout', 'ref': 'v4', 'comment': ''}]
    """
    violations: list[dict[str, str]] = []
    for file_path, slug, ref, comment in action_refs:
        if not _SHA_RE.match(ref):
            violations.append(
                {"file": file_path, "slug": slug, "ref": ref, "comment": comment}
            )
    return violations


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

    Returns ``None`` when neither source has content (first review round)."""
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


class ReviewStage(Stage):
    """Check out the target branch and perform automated code review on the ticket's implemented changes."""

    name = "review"
    input_state = State.CODE_REVIEW
    traced = True

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process a CODE_REVIEW ticket: refresh the clone, check out the ticket branch, and run the automated reviewer agent against the diff."""
        s = ctx.settings

        ws = ctx.service.workspace(ticket)

        # Resolve the implemented clone(s) — single-repo (ws.dir/"repo")
        # or meta multi-repo (ws.dir/"repos/<id>" + touched_repos.json).
        repos = implemented_repos(ws, s, ticket)
        if not repos:
            return Outcome(
                State.BLOCKED,
                "no repository clone to review (re-run implement)",
            )

        target_branch = target_branch_for(s, ctx.repo_config)

        # Compute the combined diff across every implemented clone. Each
        # repo is fetched with a freshly-minted token for ITS forge (the
        # baked-in clone token expires ~1h after clone, so a stale origin
        # URL would 401 on the fetch). For >1 repo, prefix each repo's
        # diff with a header so the reviewer can tell them apart.
        try:
            diff = combined_diff(s, ctx.repo_config, repos, target_branch)
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

        # --- stage-outcome cache: short-circuit when input is unchanged ---
        from ._stage_cache import _check, _update, review_input_hash

        input_hash = review_input_hash(ws, diff)
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

        # Bound the combined diff before it reaches the review prompt. The
        # raw ``git diff origin/<target>...HEAD`` can balloon to megabytes
        # (divergent base, generated/lockfile churn, accumulated branch
        # history) regardless of how few lines the intended change touches,
        # overflowing even a 1M-token model context. Middle-truncate so both
        # early and late files keep representation. 0 disables the cap.
        from ..core.text_utils import head_tail_keep

        diff = head_tail_keep(diff, s.review_diff_max_chars, label="git-diff")

        spec = ws.read_description()

        prior_context = _build_prior_context(ticket, ctx, ws)

        # Pre-seed the review agent with every modified file's content —
        # the reviewer otherwise burns one read_file round-trip per file
        # to verify claims, which is most of its observation count on
        # any non-trivial diff. See fs_tools.build_preseed_history.
        # ``modified_paths`` was derived above from the untruncated diff.

        # A board screenshot (captured by the smoke gate for UI-touching
        # tickets) lands at artifacts/board.png. When present, hand it to
        # the reviewer so a vision-capable backend can assess the rendered
        # board; when absent, pass None and review behaves as today.
        board_png = ws.artifacts_dir / "board.png"
        screenshot_path = board_png if board_png.exists() else None

        # ── cross-repo reusable-workflow access ──────────────────────
        # When the diff references sibling-repo workflows via ``uses:``,
        # clone those repos so the review agent can verify their
        # interface via read_file.  Clones land under .review-roots/
        # (ephemeral — discarded with the workspace).  Gracefully skip
        # repos that can't be found or cloned.
        #
        # *workflow_refs* was derived above from the UNTRUNCATED diff
        # (same reasoning as modified_paths — truncation would silently
        # drop ``uses:`` lines from the middle of the diff).
        extra_roots: list[Path] | None = None

        if workflow_refs:
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

            if workflow_refs:
                clone_roots: list[Path] = []

                # Resolve refs via repos config: for each referenced
                # owner/repo, pick up an existing clone (meta-layout
                # or prior .review-roots clone) or clone fresh.
                # Only repos whose slug matches a workflow_ref are
                # included — unlike the earlier version that blindly
                # added every meta-layout child directory.
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

                if clone_roots:
                    extra_roots = clone_roots

        # ── end cross-repo setup ─────────────────────────────────────

        # Run the blind review agent.
        try:
            verdict: ReviewVerdict = run_review_agent(
                settings=s,
                diff=diff,
                spec=spec,
                prior_context=prior_context,
                repo_dir=repo_dir,
                reference_files=modified_paths,
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

        # ── action-ref SHA-pin validation ─────────────────────────────
        # Deterministic stage-side check: every action ``uses:``
        # reference in the diff must be pinned to a full 40-char commit
        # SHA.  The LLM reviewer cannot validate SHAs (no network /
        # run_command tools), so we enforce this here and inject any
        # violations as synthetic REQUEST_CHANGES entries.
        action_violations = _validate_action_refs(action_refs)

        # Optional best-effort existence check for format-valid SHAs:
        # for each ref that IS a 40-char hex SHA, confirm it exists via
        # ``git ls-remote``.  Any failure (network error, timeout,
        # non-zero exit) degrades gracefully — the SHA is not flagged.
        for file_path, slug, ref, comment in action_refs:
            if _SHA_RE.match(ref):
                parts = slug.split("/")
                if len(parts) >= 2:
                    owner_repo = f"{parts[0]}/{parts[1]}"
                    exists = _verify_action_sha(owner_repo, ref)
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
                title = (
                    f"Pin {v['slug']} to a full 40-char commit SHA in {v['file']}"
                )[:80]
                description = (
                    f"Action `{v['slug']}@{v['ref']}{comment_part}` in "
                    f"`{v['file']}` is not pinned to a full 40-character "
                    f"commit SHA. Replace `@{v['ref']}` with a real 40-char "
                    f"commit SHA and add a `# <version>` comment, e.g. "
                    f"`uses: {v['slug']}@<full-40-char-sha> # <version>`."
                )
                synthetic_asks.append(
                    ReviewAsk(
                        title=title,
                        description=description,
                        files_touched=[v["file"]],
                    )
                )

            # Force REQUEST_CHANGES regardless of LLM verdict.  SHA-pin
            # format is a hard rule — not a discussion topic.
            verdict.verdict = "REQUEST_CHANGES"
            verdict.auto_merge_eligible = False
            verdict.request_changes = synthetic_asks + list(verdict.request_changes)
            if verdict.comments:
                verdict.comments = (
                    "Action SHA-pin validation failed (see request_changes "
                    "entries below).\n\n" + verdict.comments
                )
            else:
                verdict.comments = (
                    "Action SHA-pin validation failed (see request_changes "
                    "entries below)."
                )

        # Persist review artifact for downstream consumers (e.g. auto-merge).
        ws.artifacts_dir.joinpath("review.md").write_text(
            f"verdict: {verdict.verdict}\n"
            f"auto_merge_eligible: {str(verdict.auto_merge_eligible).lower()}\n"
            f"board_screenshot: {'present' if screenshot_path else 'absent'}\n"
            f"comment: {_collapse_comments(verdict.comments)}\n",
            encoding="utf-8",
        )

        # Route based on verdict.
        if verdict.verdict == "APPROVE":
            ctx.service.set_review_rounds(ticket.id, 0)
            outcome = Outcome(State.DOCUMENTING, "review approved")
            if input_hash:
                _update(ws, ReviewStage.name, input_hash, outcome)
            return outcome
        elif verdict.verdict == "REQUEST_CHANGES":
            rounds = ticket.review_rounds + 1
            ctx.service.set_review_rounds(ticket.id, rounds)
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
                if input_hash:
                    _update(ws, ReviewStage.name, input_hash, outcome)
                return outcome
            # --- convergence detection: repeated findings fingerprint ---
            # If the structured review asks are IDENTICAL to the previous
            # round's asks, implement is not making progress — escalate
            # early (BLOCKED) rather than burning another full cycle.
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
                if input_hash:
                    _update(ws, ReviewStage.name, input_hash, outcome)
                return outcome
            fp_path.parent.mkdir(parents=True, exist_ok=True)
            fp_path.write_text(fingerprint, encoding="utf-8")
            # Split asks against the ticket's file_map. An out-of-scope ask
            # touches files outside the ticket's declared scope; making the
            # implement agent edit them would get bounced by scope-triage and
            # loop forever. Spawn each as a SEPARATE ticket — but wire it as a
            # FOLLOW-UP that depends on THIS ticket, NOT as a prerequisite the
            # parent waits on.
            #
            # Direction matters: most out-of-scope asks are refinements of the
            # parent's OWN new code (e.g. "harden the parser this ticket
            # adds"). Such work can only run once the parent is merged, so
            # parking the parent on it deadlocks — the parent waits for the
            # child, but the child cannot act until the parent's (unmerged)
            # code exists (the 104b/413d incident: review spawned a follow-up
            # as a prerequisite and froze both). Making the child depend on the
            # parent means it runs AFTER the parent merges, operating on merged
            # code, and never gates the parent.
            file_map = _load_file_map(ws)
            in_scope, out_of_scope = _split_asks(
                verdict.request_changes,
                file_map,
            )
            # Filter out-of-scope asks: some gaps may already be addressed
            # in the implementer's branch diff.  If every file an ask would
            # touch already appears in modified_paths, the implementer
            # likely handled it inline — skip the follow-up and post a note.
            already_addressed: list[ReviewAsk] = []
            still_out_of_scope: list[ReviewAsk] = []
            if out_of_scope:
                already_addressed, still_out_of_scope = _gaps_already_addressed(
                    out_of_scope,
                    modified_paths,
                )

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
                ctx.service.add_comment(
                    ticket.id,
                    "\n".join(lines),
                    author="review",
                )

            if still_out_of_scope:
                new_ids = _spawn_dependency_tickets(
                    ticket,
                    still_out_of_scope,
                    ctx,
                )
                for nid in new_ids:
                    # Follow-up: the spawned ticket waits for THIS ticket to
                    # land (do NOT park the parent on it — that deadlocks).
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
                ctx.service.add_comment(
                    ticket.id,
                    "\n".join(lines),
                    author="review",
                )

            if in_scope:
                # In-scope changes remain — re-implement just those; the
                # out-of-scope asks are now follow-ups and are not re-asked.
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
                if input_hash:
                    _update(ws, ReviewStage.name, input_hash, outcome)
                return outcome

            if still_out_of_scope:
                # No in-scope changes: the ticket's own work is sound and the
                # only asks were out-of-scope (now follow-ups). Approve so it
                # can merge and release them — rather than parking it on work
                # that cannot run until it merges.
                ctx.service.set_review_rounds(ticket.id, 0)
                outcome = Outcome(
                    State.DOCUMENTING,
                    f"approved; {len(still_out_of_scope)} out-of-scope "
                    "ask(s) spawned as follow-ups",
                )
                if input_hash:
                    _update(ws, ReviewStage.name, input_hash, outcome)
                return outcome

            if already_addressed:
                # Every out-of-scope ask was already addressed by the
                # implementer — nothing to spawn, nothing in-scope to fix.
                # Approve directly so the ticket can merge.
                ctx.service.set_review_rounds(ticket.id, 0)
                outcome = Outcome(
                    State.DOCUMENTING,
                    f"approved; {len(already_addressed)} review gap(s) "
                    "already addressed in the implementer's commits",
                )
                if input_hash:
                    _update(ws, ReviewStage.name, input_hash, outcome)
                return outcome

            # REQUEST_CHANGES with no actionable asks — historical behaviour:
            # re-implement against the narrative comments.
            ctx.service.add_comment(
                ticket.id, _sanitize_comments(verdict.comments), author="review"
            )
            outcome = Outcome(State.READY, verdict.comments)
            if input_hash:
                _update(ws, ReviewStage.name, input_hash, outcome)
            return outcome
        else:  # NEEDS_DISCUSSION
            # A genuine human-decision verdict (e.g. "AC5 vs the 13
            # pre-existing bandit findings — pick one of 3 options").
            # This is NOT an error, so it must NOT BLOCK (which reads
            # as a failure and needs a manual resume). Pause for the
            # operator's reply instead: post the verdict as an
            # [ASK_USER] thread and route to AWAITING_USER_REPLY. When
            # the operator answers and closes the thread,
            # _maybe_resume_awaiting_user_reply auto-resumes the ticket
            # to CODE_REVIEW (paused_from) and review re-runs — this
            # time with the operator's decision visible in
            # prior-context (see _build_prior_context, which keeps
            # closed [ASK_USER] threads).
            ctx.service.add_comment(
                ticket.id,
                f"[ASK_USER]\n\n{verdict.comments}",
                author="review",
            )
            outcome = Outcome(
                State.AWAITING_USER_REPLY,
                verdict.comments,
            )
            if input_hash:
                _update(ws, ReviewStage.name, input_hash, outcome)
            return outcome
