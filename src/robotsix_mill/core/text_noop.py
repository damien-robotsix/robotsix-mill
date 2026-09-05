"""Shared 'nothing to report' detection — single source of truth.

Used by BOTH the retrospect stage (its draft-spawn guard) and the
generic ``report_issue`` tool (its self-report guard) so the two can
never drift. Title-only by design: a genuine improvement/issue title
never contains these phrases, while legitimately terse real tickets
must NOT be filtered by length heuristics.
"""

from __future__ import annotations

# Lower-case substrings that mark a report as a non-actionable
# "everything is fine" no-op rather than a real improvement/issue.
NOOP_MARKERS: tuple[str, ...] = (
    "no notable issue",
    "no issues",
    "no issue",
    "clean run",
    "nothing to flag",
    "nothing to report",
    "no improvement",
    "no action needed",
    "no concerns",
    "no notable finding",
    "all good",
    "no changes needed",
    "clean ticket",
    "nothing notable",
)


# Lower-case substrings that mark a title as a completion-announcement
# no-op — the agent is declaring it's finished, not filing an issue.
COMPLETION_ANNOUNCEMENT_MARKERS: tuple[str, ...] = (
    "refine complete",
    "spec produced",
    "refinement complete",
)

# Lower-case phrases that indicate a spec or draft explicitly mandates a
# non-empty diff — the ticket MUST produce file changes, so an empty-diff
# DONE or "no change needed" conclusion is a false close.  Mirrors the
# ``_rationale_claims_external_fix`` idiom in the refine stage: cheap,
# deterministic, low false-positive.
CODE_CHANGE_MANDATE_PHRASES: tuple[str, ...] = (
    "non-empty diff",
    "nonempty diff",
    "non-empty change",
    "must produce a diff",
    "must produce a non-empty",
    "must produce changes",
    "must make changes",
    "must change at least one file",
    "must modify at least one file",
    "must not produce an empty diff",
    "must not result in an empty diff",
    "must not close without",
    "must not be closed without",
    "must not mark as no-change",
    "must not conclude no change",
    "must not conclude 'already satisfied'",
    "must result in a non-empty diff",
    "empty diff is not acceptable",
    "an empty diff is not acceptable",
    "forbidden to close without changes",
    "required artifacts",
    "required files",
    "required deliverables",
    "must create the following",
    "must create a file",
    "must add the following files",
)


def is_completion_announcement(title: str | None) -> bool:
    """True if *title* is a completion-announcement no-op."""
    t = (title or "").strip().lower()
    if not t:
        return False
    return any(m in t for m in COMPLETION_ANNOUNCEMENT_MARKERS)


def is_noop_report(title: str | None) -> bool:
    """True if *title* is an empty or 'nothing to report' no-op."""
    t = (title or "").strip().lower()
    if not t:
        return True
    return any(m in t for m in NOOP_MARKERS)


def spec_demands_code_change(text: str | None) -> bool:
    """Return True when *text* explicitly forbids an empty-diff conclusion.

    Detects spec/draft language that mandates a non-empty diff or the
    creation of required artifacts (files, endpoints, deliverables).  When
    this fires, an empty-diff DONE (implement fast-path) or a refine
    ``no_change_needed`` close would contradict the spec, so the caller
    must route the ticket to a real implement pass or block it for
    inspection instead of silently closing.

    Bias is toward NOT firing: only unambiguous mandate phrases match, so
    legitimate no-change subclasses (information-only deliverables,
    detector false-positives, sibling-ticket cleanups) keep closing.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(phrase in t for phrase in CODE_CHANGE_MANDATE_PHRASES)


# Lower-case substrings that mark a BODY as a placeholder pointer rather
# than real content. Lives here rather than in the refine stage because
# two very different callers need the same answer: refine (is this spec
# usable?) and the ticket-spawning agents (is this body worth filing?).
PLACEHOLDER_BODY_PHRASES: tuple[str, ...] = (
    "see spec above",
    "see the spec above",
    "see above",
    "see spec",
    "see the spec",
    "see description",
    "see the description",
    "spec above",
    "as above",
    "as written above",
    "see previous",
    "see below",
    "refer to spec",
    "tbd",
    "todo",
)


def degenerate_body_reason(text: str | None) -> str | None:
    """Return a human-readable reason *text* is degenerate, or ``None``.

    Companion to :func:`is_degenerate_body` that explains WHY the body
    was rejected — so refine block notes can distinguish real spec gaps
    from false negatives (e.g. a 13k-char prescriptive draft that the
    refiner returned as a placeholder pointer).

    Returns a short sentence (no trailing period) like ``"body is empty"``,
    ``"body is only punctuation"``, or ``"body matches placeholder phrase
    'tbd'"``.  Returns ``None`` when the body passes all checks.
    """
    import re

    stripped = (text or "").strip()
    if not stripped:
        return "body is empty"
    if len(stripped) > 120:
        return None
    norm = " ".join(re.sub(r"[^a-z0-9 ]+", " ", stripped.lower()).split())
    if not norm:
        return "body is only punctuation"
    for p in PLACEHOLDER_BODY_PHRASES:
        if p in norm:
            return f"body matches placeholder phrase {p!r}"
    return None


def is_degenerate_body(text: str | None) -> bool:
    """True when *text* is empty or a placeholder pointer, not real content.

    Only short (≤120-char) single-idea strings can match; a genuine body is
    much longer, so real content is never dropped. Punctuation-only strings
    normalise to empty and count as degenerate — a retrospect draft whose
    body was literally ``"..."`` reached the board and then blocked in
    refine, which is what this guards.
    """
    return degenerate_body_reason(text) is not None


# Fully-normalized titles (punctuation stripped, whitespace collapsed,
# lower-cased) that mark a ticket as a throwaway/test fixture rather than
# real work.  An agent that (mis)uses ``report_issue`` — or any creation
# path — to make itself a quick fixture ticket produces one of these,
# never a genuine blocking issue.  Two such tickets (``noop-8835``,
# ``dummy-2218``) leaked onto production boards from implement sessions,
# with ``source=agent`` and empty/placeholder descriptions; one flowed
# through refine into a real (wasted) implement run before a monitor
# closed it.  Matched against the WHOLE normalized title so real titles
# that merely contain such a word survive ("Fix flaky test in X"
# normalizes to "fix flaky test in x", not "test").
PLACEHOLDER_TITLE_TOKENS: frozenset[str] = frozenset(
    {
        "noop",
        "no op",
        "dummy",
        "dummy ticket",
        "test",
        "tests",
        "testing",
        "test ticket",
        "test123",
        "placeholder",
        "placeholder ticket",
        "disregard",
        "disregard placeholder",
        "ignore",
        "ignore me",
        "ignore this",
        "delete",
        "delete me",
        "deleteme",
        "throwaway",
        "throw away",
        "sample",
        "example",
        "foo",
        "bar",
        "baz",
        "asdf",
        "qwerty",
        "xxx",
        "tmp",
        "temp",
        "junk",
        "scratch",
    }
)


def _normalize_title_words(title: str | None) -> str:
    """Lower-case *title*, strip non-alphanumerics to spaces, collapse."""
    import re

    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).split())


def is_placeholder_ticket(title: str | None, body: str | None = None) -> bool:
    """True when *(title, body)* is an obvious placeholder/test fixture.

    Fires when the fully-normalized *title* is a bare throwaway token
    (``noop``, ``dummy``, ``test``, ``placeholder``, ``disregard``, …), or
    when a title that merely *starts* with such a token is paired with an
    empty/placeholder *body* (e.g. title ``"dummy ticket"`` + body
    ``"disregard placeholder"``).  This is the real data shape of the junk
    agent tickets that must never enter the pipeline.  An empty title is
    itself a placeholder.

    Bias is toward NOT firing: a normal terse title survives because only
    the enumerated throwaway tokens match, and the body-widened branch
    additionally requires a degenerate body.
    """
    norm = _normalize_title_words(title)
    if not norm:
        return True
    if norm in PLACEHOLDER_TITLE_TOKENS:
        return True
    first = norm.split(" ", 1)[0]
    return first in PLACEHOLDER_TITLE_TOKENS and is_degenerate_body(body)
