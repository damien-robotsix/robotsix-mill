"""Class-based routing policy for auto-proposed CI / dev-tooling additions.

Survey/audit periodic agents repeatedly file near-identical "add a new
standing CI gate / dev-tooling standard" proposals (a coverage gate, an
OpenSSF Scorecard workflow, a secret scanner, a cyclomatic-complexity
gate, …).  Each is a *workflow-policy* decision, so today each lands in
``HUMAN_ISSUE_APPROVAL`` and costs the operator a separate escalation
panel — a steady, un-drainable stream of approval gates.

This module turns that per-ticket operator decision into a *configurable,
class-based policy*.  A proposal is classified into a canonical
tool-class id (``coverage``, ``scorecard``, ``secret-scanner``, …); the
operator maps classes to routes in config:

* **allowlist** — a class mapped to ``auto-ready`` routes straight to
  implement without a human gate.
* **denylist** — a class mapped to ``auto-decline`` auto-closes with a
  note, fleet-wide.
* **default** — unlisted classes follow ``ci_tooling_proposal_default``
  (``escalate`` — the human gate, today's behaviour — or ``auto-decline``).

Adding or removing a class from the allow/deny set is a config edit, not
a code change.  With an empty policy and the default route of
``escalate`` the behaviour is identical to today (every CI-tooling
proposal reaches the human gate), so the mechanism is a no-op until the
operator supplies the lists.
"""

from __future__ import annotations

from enum import StrEnum

from ...config.settings import Settings


class CIToolingRoute(StrEnum):
    """The three routes an auto-proposed CI/dev-tooling class can take."""

    AUTO_READY = "auto-ready"
    AUTO_DECLINE = "auto-decline"
    ESCALATE = "escalate"


#: Canonical CI/dev-tooling proposal classes mapped to the case-insensitive
#: substrings (matched against *title* + *body*) that identify them.  Kept
#: deliberately narrow so an ordinary code-quality draft is not misread as
#: a standing-gate proposal.  Order matters only for overlapping terms:
#: the first class whose term matches wins.
CI_TOOLING_CLASS_TERMS: dict[str, tuple[str, ...]] = {
    "secret-scanner": (
        "secret scanner",
        "secret scanning",
        "gitleaks",
        "trufflehog",
    ),
    "sast": ("codeql", "semgrep", "static analysis gate"),
    "dependency-scanner": (
        "trivy",
        "dependabot",
        "dependency scanning",
        "dependency scanner",
    ),
    "scorecard": ("openssf scorecard", "scorecard workflow"),
    "coverage": (
        "coverage gate",
        "code coverage measurement",
        "coverage measurement",
        "code-coverage",
    ),
    "complexity": (
        "radon",
        "xenon",
        "cyclomatic complexity",
        "complexity gate",
    ),
    "visual-regression": ("visual regression", "chromatic"),
    "conventional-commits": ("conventional commits", "commitizen"),
    "benchmark": (
        "performance benchmark",
        "performance benchmarks",
        "benchmark in ci",
        "benchmarks in ci",
    ),
}


def classify_ci_tooling_proposal(title: str, body: str) -> str | None:
    """Return the canonical CI/dev-tooling class for a proposal, or ``None``.

    A conservative keyword match over *title* + *body*; returns the first
    class in :data:`CI_TOOLING_CLASS_TERMS` whose any term appears. Returns
    ``None`` when the draft is not a recognised CI/dev-tooling proposal so
    the caller falls through to its normal triage path.
    """
    hay = f"{title}\n{body}".lower()
    for class_id, terms in CI_TOOLING_CLASS_TERMS.items():
        if any(term in hay for term in terms):
            return class_id
    return None


def resolve_ci_tooling_route(class_id: str, settings: Settings) -> CIToolingRoute:
    """Resolve the configured :class:`CIToolingRoute` for *class_id*.

    A per-class mapping in ``settings.ci_tooling_proposal_policy`` takes
    precedence (allowlist → ``auto-ready``, denylist → ``auto-decline``).
    An unlisted class — or an unrecognised configured value — falls back
    to ``settings.ci_tooling_proposal_default`` (and to
    :data:`CIToolingRoute.ESCALATE` if that too is unrecognised), so a
    typo never silently auto-closes or auto-approves a proposal.
    """
    override = (settings.ci_tooling_proposal_policy or {}).get(class_id)
    if override is not None:
        try:
            return CIToolingRoute(override)
        except ValueError:
            pass

    try:
        return CIToolingRoute(settings.ci_tooling_proposal_default)
    except ValueError:
        return CIToolingRoute.ESCALATE
