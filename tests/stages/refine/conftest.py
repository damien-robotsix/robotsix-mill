"""Bridge fixtures adapting the collapsed pre/post-refine seams to the
legacy per-agent seams these tests mock.

The gate-chain collapse (stan-2619) replaced the serial
``triage_refine`` + ``run_dedup_check`` and ``review_spec_for_conciseness``
+ ``triage_auto_approve`` calls with two combined seams
(``run_pre_refine_classifier`` / ``run_post_refine_check``).  The tests in
this package still express intent through the legacy seams, so these
autouse fixtures delegate the combined calls to whatever the test has
patched onto the legacy attributes — read at call time, so per-test
``monkeypatch.setattr`` overrides keep working unchanged.  Tests that
patch the combined seams directly win, because their setattr lands after
the fixture's.
"""

import pytest

from robotsix_mill.agents import dedup, post_refine, pre_refine_classifier, refining
from robotsix_mill.agents.post_refine import PostRefineResult
from robotsix_mill.agents.pre_refine_classifier import PreRefineClassifierResult
from robotsix_mill.agents.refine_triage import TriageResult


@pytest.fixture(autouse=True)
def _bridge_pre_refine_classifier(monkeypatch):
    def _bridge(
        *, settings, title, draft, candidates_json="", reviewer_comments=None, **kw
    ):
        # Reviewer sendback: the legacy gate never ran triage/dedup here
        # (human-flagged changes always refine); only the reviewer
        # agreement assessment applies.
        if reviewer_comments:
            agreement = None
            reason = ""
            try:
                ra = refining.triage_reviewer_agreement(
                    settings=settings,
                    draft=draft,
                    reviewer_comments=reviewer_comments,
                )
                agreement, reason = ra.decision, ra.reason
            except Exception:
                pass
            return PreRefineClassifierResult(
                triage_decision="REFINE",
                triage_reason="reviewer sendback",
                reviewer_agreement=agreement,
                reviewer_agreement_reason=reason,
            )
        # Mirror the real classifier's graceful degradation: any failure
        # yields a REFINE / no-duplicate result instead of propagating.
        if getattr(settings, "refine_triage_enabled", True):
            try:
                triage = refining.triage_refine(
                    settings=settings, title=title, draft=draft
                )
            except Exception:
                triage = TriageResult(
                    decision="REFINE",
                    reason="classifier failed — proceeding with refine",
                )
        else:
            triage = TriageResult(decision="REFINE", reason="triage disabled")
        if candidates_json:
            try:
                verdict = dedup.run_dedup_check(
                    settings=settings,
                    draft_title=title,
                    draft_body=draft,
                    candidates_json=candidates_json,
                )
            except Exception:
                verdict = {"duplicate_of": None, "already_done": None, "reason": ""}
        else:
            verdict = {"duplicate_of": None, "already_done": None, "reason": ""}
        if not isinstance(verdict, dict):
            verdict = {
                "duplicate_of": getattr(verdict, "duplicate_of", None),
                "already_done": getattr(verdict, "already_done", None),
                "reason": getattr(verdict, "reason", ""),
            }
        return PreRefineClassifierResult(
            triage_decision=triage.decision,
            triage_reason=triage.reason,
            target_board=triage.target_board,
            complexity=triage.complexity,
            trivial_scope=triage.trivial_scope,
            exploration_findings=triage.exploration_findings,
            duplicate_of=verdict.get("duplicate_of"),
            already_done=verdict.get("already_done"),
            dedup_reason=verdict.get("reason") or "",
        )

    monkeypatch.setattr(pre_refine_classifier, "run_pre_refine_classifier", _bridge)


@pytest.fixture(autouse=True)
def _bridge_post_refine(monkeypatch):
    def _bridge(*, settings, spec, reviewer_comments=None, **kw):
        review = refining.review_spec_for_conciseness(
            settings=settings, spec_markdown=spec
        )
        try:
            approve = refining.triage_auto_approve(settings=settings, spec=spec)
            decision, reason = approve.decision, approve.reason
        except RuntimeError as exc:
            # An unmocked seam hits the real-call guard: keep the review
            # result and fall back to human approval, as the legacy flow
            # did when auto-approve failed. Deliberate mock exceptions
            # (non-guard) propagate into _run_post_refine_check's
            # degrade path so the legacy fallback re-raises them.
            if "Blocked real" not in str(exc):
                raise
            decision, reason = (
                "NEEDS_APPROVAL",
                "auto-approve triage failed — falling back to human approval",
            )
        return PostRefineResult(
            concise_spec=review.concise_spec,
            stripped_summary=review.stripped_summary,
            auto_approve=decision,
            auto_approve_reason=reason,
        )

    monkeypatch.setattr(post_refine, "run_post_refine_check", _bridge)
