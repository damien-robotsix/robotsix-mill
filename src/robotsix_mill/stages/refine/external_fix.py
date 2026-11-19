"""External-fix claim detection and live branch-merged verification for the
refine stage.

Owns the banner-delimited "external-fix claim detection (live re-verification
gate)" block, split out of ``helpers.py``: the detector that fires when a
refine ``no_change_needed`` verdict asserts the work was already shipped
elsewhere, plus the live ancestry checks (``_verify_cited_fix_at_head`` and
``_verify_branch_merged``) that re-verify such a claim so the stage routes
the ticket to implement for a live re-check instead of closing to DONE.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from ...core.models import Ticket
from ...scm.vcs import git_ops

# Same logger name as helpers.py's ``log`` so external-fix records keep
# emitting under ``robotsix_mill.stages.refine`` (helpers.py cannot re-export
# its own ``log`` here without creating a circular import).
log = logging.getLogger("robotsix_mill.stages.refine")

# --- external-fix claim detection (live re-verification gate) ---------------
# A refine ``no_change_needed`` verdict that asserts the work was *already
# shipped elsewhere* must NOT be trusted on its word: the 2026-06-09 incident
# closed a live CI-failure ticket as a duplicate while the fix had been
# reverted at HEAD. ``git merge-base --is-ancestor`` cannot detect that
# (the reverted fix commit is still an ancestor). So when this detector
# fires, the stage routes the ticket to implement for a live re-check
# instead of closing to DONE.

# Unambiguous "already shipped elsewhere" phrases — any single one fires.
_EXTERNAL_FIX_PHRASES: tuple[str, ...] = (
    "already implemented",
    "already fixed",
    "already shipped",
    "already merged",
    "already applied",
    "already resolved",
    "already done",
    "duplicate of",
    "shipped the fix",
    "parallel ticket",
    # "Superseded by / replaced by <other ticket>" means the work was
    # deliberately moved elsewhere and NEVER done here — the inverse of a
    # valid dedup target. Live: dde5 was mark-done'd "Superseded …
    # Replaced by the deterministic-closure ticket", then dedup closed
    # that very replacement (6a68) as a duplicate OF dde5, annihilating
    # the capability entirely (2026-09-05, robotsix-mill board).
    "superseded",
    "replaced by",
)

# Repo ticket-id shape and commit-SHA-like token.
_TICKET_ID_RE = re.compile(r"\b\d{8}T\d{6}Z\b")
_COMMIT_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
# PR / MR number references: e.g. "#1386", "!123", "PR #42".
_PR_MR_REF_RE = re.compile(r"(?<!\w)(?:#|!)\d+\b")

# Resolved-claim verbs that, co-occurring with a cited ticket id / commit /
# PR/MR reference, imply an external-fix claim even without one of the
# canned phrases.
_RESOLVED_VERB_RE = re.compile(
    r"\b(implemented|fixed|shipped|merged|applied|resolved|addressed|landed)\b"
)

# Markers of the two legitimate no-change subclasses that must KEEP closing
# to DONE: detector false-positives and information-only deliverables. They
# suppress only the fuzzy ref+verb co-occurrence rule (never an unambiguous
# external-fix phrase).
_FALSE_POSITIVE_MARKERS: tuple[str, ...] = (
    "does not exist",
    "doesn't exist",
    "false positive",
    "disproves",
    "not actually",
    "cannot reproduce",
    "can't reproduce",
)
_INFO_ONLY_MARKERS: tuple[str, ...] = (
    "post a comment",
    "documenting",
    "information-only",
    "informational",
    "explaining why",
)


def _rationale_claims_external_fix(rationale: str) -> bool:
    """Return ``True`` when *rationale* asserts the work is already done
    elsewhere (so the fix's live presence must be re-verified, not trusted).

    Fires on an unambiguous "already shipped elsewhere" phrase, or on a
    cited ticket id / commit SHA / PR-MR number co-occurring with a
    resolved-claim verb.
    Returns ``False`` for the two legitimate no-change subclasses — detector
    false-positives and information-only deliverables — so they keep closing
    to DONE.  Empty/whitespace rationale → ``False``.  Bias is toward NOT
    firing except when an external-fix verb is unambiguously present.
    """
    text = (rationale or "").strip().lower()
    if not text:
        return False

    # Unambiguous external-fix phrase → fire regardless of other markers.
    for phrase in _EXTERNAL_FIX_PHRASES:
        if re.search(r"\b" + re.escape(phrase) + r"\b", text):
            return True

    # The two legitimate subclasses suppress the fuzzy co-occurrence rule.
    if any(m in text for m in _FALSE_POSITIVE_MARKERS) or any(
        m in text for m in _INFO_ONLY_MARKERS
    ):
        return False

    # Cited ticket id / commit SHA / PR-MR number co-occurring with a
    # resolved-claim verb.
    has_ref = bool(
        _TICKET_ID_RE.search(text)
        or _COMMIT_SHA_RE.search(text)
        or _PR_MR_REF_RE.search(text)
    )
    return bool(has_ref and _RESOLVED_VERB_RE.search(text))


def _verify_cited_fix_at_head(repo_dir: Path | None, rationale: str) -> bool:
    """Best-effort: whether a SHA cited in *rationale* is a valid commit that
    is an ancestor of ``origin/main``.

    For logging/enrichment only — it MUST NOT short-circuit back to DONE,
    because ancestry passing does not prove the fix is live (a revert leaves
    the original fix commit as an ancestor while the bug is back). Mirrors the
    defensive subprocess style of ``_verify_branch_merged``: any git error is
    swallowed and the function returns ``False`` (nothing proven).
    """
    if repo_dir is None:
        return False
    shas = _COMMIT_SHA_RE.findall((rationale or "").lower())
    if not shas:
        return False
    # Refresh origin/main before the ancestry assertion so a stale clone
    # cannot report a cited commit as missing just because it never fetched.
    git_ops.refresh_origin_target(repo_dir, "main")
    try:
        for sha in shas:
            type_check = subprocess.run(
                ["git", "-C", str(repo_dir), "cat-file", "-t", sha],
                capture_output=True,
                text=True,
            )
            if type_check.returncode != 0 or type_check.stdout.strip() != "commit":
                continue
            anc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "merge-base",
                    "--is-ancestor",
                    sha,
                    "origin/main",
                ],
                capture_output=True,
                text=True,
            )
            if anc.returncode == 0:
                return True
    except Exception:
        log.debug(
            "cited-fix ancestry check failed for rationale — ignoring",
            exc_info=True,
        )
    return False


def _verify_branch_merged(repo_dir: Path | None, ticket: Ticket) -> bool:
    """Check whether *ticket*'s branch is an ancestor of the base branch.

    When a redrafted ticket has a branch from a prior implement run,
    the refine agent may conclude ``no_change_needed`` because the
    implementation already exists — but if the branch was never merged
    to the base branch, closing as DONE strands the work on an orphaned
    branch.

    Returns ``True`` when the branch is confirmed merged to the base
    branch, or when the check cannot be performed (best-effort: we
    never block a ticket on a transient git error).

    Returns ``False`` only when the branch is confirmed **unmerged**
    — i.e. ``git merge-base --is-ancestor <branch> origin/main`` exits 1.

    When the branch cannot be fetched from origin (e.g. it was committed
    locally by a prior implement run but never pushed), fall back to the
    **local** branch ref ``refs/heads/<branch>`` and check its ancestry
    against ``origin/main``.  Only when neither an origin branch nor a
    local ref can be resolved do we best-effort ``return True`` — there
    is then genuinely nothing to verify.
    """
    if repo_dir is None or not ticket.branch:
        # Nothing to verify — let the no-change-needed pass through.
        return True

    branch = ticket.branch
    # Refresh origin/main first: every "unmerged" verdict below compares
    # against origin/main, and a stale clone must not turn a freshly merged
    # branch into a false BLOCKED ticket.
    git_ops.refresh_origin_target(repo_dir, "main")
    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "origin", branch],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        # The branch is absent from origin (fetch failed). It may still
        # exist as a local-only ref committed by a prior implement run
        # that never pushed. Fall back to the local ref before allowing
        # the no-change-needed pass-through — otherwise a complete,
        # working feature strands on an orphaned local WIP commit.
        log.debug(
            "%s: cannot fetch branch '%s' from origin — "
            "falling back to local ref for merge check",
            ticket.id,
            branch,
        )
        local_ref = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
            text=True,
        )
        if local_ref.returncode != 0:
            # Neither an origin branch nor a local ref — nothing to
            # verify, best-effort allow.
            log.debug(
                "%s: branch '%s' resolves on neither origin nor locally "
                "— allowing no-change-needed (best-effort)",
                ticket.id,
                branch,
            )
            return True
        local_check = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "merge-base",
                "--is-ancestor",
                f"refs/heads/{branch}",
                "origin/main",
            ],
            capture_output=True,
            text=True,
        )
        if local_check.returncode == 0:
            return True  # local branch is merged
        if local_check.returncode == 1:
            # Squash-merge detection: search main for a commit
            # referencing this ticket.
            grep = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "log",
                    "origin/main",
                    "--oneline",
                    "--fixed-strings",
                    f"--grep={ticket.id}",
                ],
                capture_output=True,
                text=True,
            )
            if grep.returncode == 0 and grep.stdout.strip():
                log.info(
                    "%s: local branch '%s' is not an ancestor of "
                    "origin/main, but a commit referencing this ticket "
                    "was found on origin/main — treating as squash-merged",
                    ticket.id,
                    branch,
                )
                return True
            log.info(
                "%s: local branch '%s' is NOT an ancestor of origin/main "
                "— implementation unmerged",
                ticket.id,
                branch,
            )
            return False  # local branch is unmerged
        # Any other exit code (git error) — best-effort, don't block.
        log.debug(
            "%s: local merge-base check failed for branch '%s' — "
            "allowing no-change-needed (best-effort)",
            ticket.id,
            branch,
        )
        return True

    # git merge-base --is-ancestor: exit 0 = is ancestor, 1 = not ancestor
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "merge-base",
            "--is-ancestor",
            f"origin/{branch}",
            "origin/main",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True  # branch is merged
    if result.returncode == 1:
        # Squash-merge detection: search main for a commit
        # referencing this ticket.
        grep = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "log",
                "origin/main",
                "--oneline",
                "--fixed-strings",
                f"--grep={ticket.id}",
            ],
            capture_output=True,
            text=True,
        )
        if grep.returncode == 0 and grep.stdout.strip():
            log.info(
                "%s: branch '%s' is not an ancestor of origin/main, "
                "but a commit referencing this ticket was found on "
                "origin/main — treating as squash-merged",
                ticket.id,
                branch,
            )
            return True
        log.info(
            "%s: branch '%s' is NOT an ancestor of main — implementation unmerged",
            ticket.id,
            branch,
        )
        return False  # branch is unmerged

    # Any other exit code (git error) — best-effort, don't block.
    log.debug(
        "%s: merge-base check failed for branch '%s' — "
        "allowing no-change-needed (best-effort)",
        ticket.id,
        branch,
    )
    return True
