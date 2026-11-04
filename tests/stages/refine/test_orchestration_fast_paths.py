"""Refine-stage orchestration: mechanical-draft fast paths, ops-shaped
retrospect gate, and internal-failure short-circuits.

Split out of ``test_orchestration.py``; module-level helpers and the
agent mock seams are imported from that parent module (which retains
them).
"""

import json

import pytest

from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import (
    RefineResult,
)
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import RefineStage

# A genuine (> 120 char) spec body so ``_spec_is_degenerate`` never trips.
from tests.stages.refine.test_orchestration import (
    _apply_default_mocks,
    _mock_refine_returns,
    _mock_triage,
    _run_agent,
    _spy_refine,
    _ticket,
)


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    from robotsix_mill.config import RepoConfig, Settings

    counter = [0]

    def make(**env):
        db.reset_engine()
        s = Settings(data_dir=str(tmp_path / f"data{counter[0]}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        counter[0] += 1
        return StageContext(
            settings=s,
            service=svc,
            repo_config=RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    db.reset_engine()


# ===========================================================================
# _triage_skip — mechanical draft fast-path
# ===========================================================================


def test_mechanical_draft_fast_path_skips_refine_agent(
    ctx_factory, monkeypatch, tmp_path
):
    """When a mill-internal automated ticket passes triage with REFINE but
    auto-approve confirms it is purely mechanical, the expensive refine
    agent is skipped entirely and the draft passes through as the spec."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="test_gap")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(
            decision="REFINE", reason="needs minor wording polish"
        ),
    )
    from robotsix_mill.agents.refining import AutoApproveResult

    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **kw: AutoApproveResult(
            decision="APPROVE",
            reason="Mechanical rename — no design decisions",
        ),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Rename `old_func` to `new_func` in `src/foo/bar.py`.",
    )

    assert out.note.startswith("mechanical draft fast-path")
    assert "APPROVE" in out.note
    assert calls == []  # full refine agent never invoked
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()
    file_map = json.loads(
        (ws.artifacts_dir / "file_map.json").read_text(encoding="utf-8")
    )
    assert {"file": "src/foo/bar.py", "note": "from draft"} in file_map


def test_mechanical_draft_fast_path_triggered_for_user_tickets(
    ctx_factory, monkeypatch, tmp_path
):
    """Human-written tickets with mechanical drafts now take the
    mechanical fast-path when auto-approve confirms no design
    decisions — the expensive refine agent is skipped."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="user")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )
    from robotsix_mill.agents.refining import AutoApproveResult

    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **kw: AutoApproveResult(
            decision="APPROVE",
            reason="Mechanical rename — no design decisions",
        ),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Rename `old_func` to `new_func` in `src/foo/bar.py`.",
    )

    # Should hit the fast-path; full refine agent should NOT be invoked.
    assert out.note.startswith("mechanical draft fast-path")
    assert "APPROVE" in out.note
    assert calls == []  # full refine agent never invoked


def test_mechanical_draft_fast_path_not_triggered_for_ci_tickets(
    ctx_factory, monkeypatch, tmp_path
):
    """CI-failure tickets never take the mechanical fast-path."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="ci")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Fix the SHA in `.github/workflows/ci.yml`.",
    )

    assert not out.note.startswith("mechanical draft fast-path")
    assert len(calls) == 1  # full refine agent WAS invoked


def test_mechanical_draft_fast_path_triggers_regardless_of_require_approval(
    ctx_factory, monkeypatch, tmp_path
):
    """The fast-path is now always active (auto-approve is always-on)."""
    ctx = ctx_factory(require_approval=False)
    t = _ticket(ctx, source="test_gap")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Rename `old_func` to `new_func` in `src/foo/bar.py`.",
    )

    assert out.note.startswith("mechanical draft fast-path")
    assert len(calls) == 0  # full refine agent NOT invoked


def test_mechanical_draft_fast_path_short_circuits_on_needs_approval(
    ctx_factory, monkeypatch, tmp_path
):
    """When auto-approve returns NEEDS_APPROVAL, the mechanical fast-path
    still short-circuits — the draft is preserved as-is and the expensive
    refine agent is skipped.  The ticket routes to HUMAN_ISSUE_APPROVAL
    via _resolved_outcome so the human reviewer sees why."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="periodic")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )
    from robotsix_mill.agents.refining import AutoApproveResult

    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **kw: AutoApproveResult(
            decision="NEEDS_APPROVAL",
            reason="New API contract introduced",
        ),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Add a new public endpoint with authentication and rate limiting.",
    )

    # Fast-path short-circuit fires even on NEEDS_APPROVAL.
    assert out.note.startswith("mechanical draft fast-path")
    assert "NEEDS_APPROVAL" in out.note
    assert "New API contract introduced" in out.note
    assert calls == []  # full refine agent was NOT invoked
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()


def test_mechanical_draft_fast_path_falls_through_on_auto_approve_error(
    ctx_factory, monkeypatch, tmp_path
):
    """When the auto-approve call raises, fall through gracefully."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="periodic")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )

    def _boom(**kw):
        raise RuntimeError("OpenRouter transient error")

    monkeypatch.setattr(refining, "triage_auto_approve", _boom)

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Rename `old_func` to `new_func` in `src/foo/bar.py`.",
    )

    assert not out.note.startswith("mechanical draft fast-path")
    assert len(calls) == 1  # full refine agent WAS invoked


def test_mechanical_draft_fast_path_falls_through_on_empty_draft(
    ctx_factory, monkeypatch, tmp_path
):
    """An empty/whitespace draft must NOT take the mechanical fast-path,
    even when auto-approve is enabled.  Otherwise an empty draft gets
    auto-approved → refine produces empty body → fast-path approves
    again → infinite loop."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="periodic")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )

    out = _run_agent(ctx, t, tmp_path, draft="   ")

    assert not out.note.startswith("mechanical draft fast-path")
    assert len(calls) == 1  # full refine agent WAS invoked


def test_mechanical_draft_fast_path_falls_through_on_empty_draft_user_source(
    ctx_factory, monkeypatch, tmp_path
):
    """Same guard for user-source tickets — empty drafts never fast-path."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="user")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )

    out = _run_agent(ctx, t, tmp_path, draft="")

    assert not out.note.startswith("mechanical draft fast-path")
    assert len(calls) == 1  # full refine agent WAS invoked


# ===========================================================================
# Ops-shaped retrospect follow-up gate
# ===========================================================================


@pytest.mark.parametrize(
    "draft",
    [
        "Run the acceptance batch: `POST /api/batch/jobs` with 20 games.",
        "Re-run the verification on an environment with adequate memory.",
        "Execute the batch against the deployed service and report results.",
    ],
    ids=["post-api", "adequate-memory", "deployed-service"],
)
def test_ops_shaped_retrospect_followup_routes_to_human_gate(
    ctx_factory, monkeypatch, tmp_path, draft
):
    """A retrospect follow-up whose draft is ops-shaped (asks to RUN
    something against a deployed service / high-memory host) is routed to
    HUMAN_ISSUE_APPROVAL ahead of the mechanical fast-path — neither
    triage_auto_approve nor the full refine agent is invoked."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="retrospect")
    calls = _spy_refine(monkeypatch)

    auto_approve_calls: list[dict] = []

    def _spy_auto_approve(**kw):
        auto_approve_calls.append(kw)
        return refining.AutoApproveResult(decision="APPROVE", reason="ok")

    monkeypatch.setattr(refining, "triage_auto_approve", _spy_auto_approve)

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    assert out.next_state == State.HUMAN_ISSUE_APPROVAL
    assert "ops-shaped retrospect follow-up" in out.note
    assert auto_approve_calls == []  # auto-approve never invoked
    assert calls == []  # full refine agent never invoked


def test_ops_shaped_gate_does_not_fire_for_user_source(
    ctx_factory, monkeypatch, tmp_path
):
    """A user-source ticket that legitimately quotes an API route in its
    spec must NOT be caught by the ops-shape gate — the existing fast-path
    behaviour is unchanged."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="user")
    _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )
    from robotsix_mill.agents.refining import AutoApproveResult

    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **kw: AutoApproveResult(
            decision="APPROVE",
            reason="Mechanical — no design decisions",
        ),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Add a handler for `POST /api/batch/jobs` in `src/foo/bar.py`.",
    )

    assert out.next_state != State.HUMAN_ISSUE_APPROVAL
    assert "ops-shaped retrospect follow-up" not in out.note
    assert out.note.startswith("mechanical draft fast-path")


def test_retrospect_followup_without_ops_markers_falls_through(
    ctx_factory, monkeypatch, tmp_path
):
    """A retrospect follow-up with no ops markers falls through to today's
    fast-path/full-refine behaviour — the gate does not fire."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="retrospect")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )
    from robotsix_mill.agents.refining import AutoApproveResult

    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **kw: AutoApproveResult(
            decision="APPROVE",
            reason="Mechanical rename — no design decisions",
        ),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Rename `old_func` to `new_func` in `src/foo/bar.py`.",
    )

    assert "ops-shaped retrospect follow-up" not in out.note
    assert out.note.startswith("mechanical draft fast-path")
    assert calls == []  # full refine agent skipped by fast-path


# ===========================================================================
# Operator-prerequisite gate + NEEDS_APPROVAL sensitive-path fail-closed
# ===========================================================================

# Verbatim description of robotsix-website ticket bb70 (survey).
_BB70_DRAFT = """\
**Gap:** `SECURITY.md` tells reporters to email `admin@robotsix.net` (lines 3-4) and has a response-time and coordinated-disclosure section (lines 6-13). It does not mention GitHub's Private Vulnerability Reporting (PVR), and it does not say what is in scope. The site has a user-submission surface (the survey feature under `www/42/`, which stores data in SQLite and the `SURVEY_DB_PATH` env var described in README.md) and a security-header configuration (`www/.htaccess`), so reporters need to know which parts are in scope.

**Comparator / reference:** GitHub's community-health guidance (https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file) recommends that SECURITY.md state how to report, the scope of accepted reports, and the expected response timeline, and that it be paired with PVR under Settings → Security. This URL came from the cached knowledge base and was not re-fetched in this run, so verify it before relying on it.

**Suggested change (repo-local, no code):**
1. Repo owner enables *Private vulnerability reporting* under Settings → Security (a settings change, not a file change).
2. In `SECURITY.md`, keep the email as a fallback and add a line pointing to `https://github.com/damien-robotsix/robotsix-website/security/advisories/new` as the preferred channel.
3. Add a short `## Scope` section: in scope = PHP pages under `www/`, the survey feature, and the HTTP headers in `www/.htaccess`; out of scope = OVH hosting infrastructure and third-party services.
4. Leave the existing response-time and coordinated-disclosure sections as they are.

**Acceptance:** `SECURITY.md` names PVR as the preferred channel and has a Scope section; the PVR toggle is on (checked in repo settings). No CI change is needed.

**Standards check:** the standards checkout was not reachable this run, so I could not confirm whether robotsix-standards already defines a security-policy convention. If it does, this ticket should follow it rather than the local wording.

Observed on: robotsix-website

<!-- survey-gap-id: security-policy-private-vulnerability-reporting -->
"""

# Verbatim description of robotsix-website ticket a4e7 (survey).
_A4E7_DRAFT = """\
**Gap (verified in tree):** the PHP runtime is not declared anywhere in the repo, so CI and deploy test against whatever PHP the self-hosted runner's apt repository provides.

- `composer.json` (whole file) has no `require.php` constraint and no `config.platform.php`. Its only keys are `name`, `description`, `require-dev` (`phpunit/phpunit ^11.0`), `config` (`sort-packages`, `allow-plugins`) and `scripts.test`.
- `.github/workflows/ci.yml:28` installs PHP with `apt-get install php-cli php-sqlite3`; `ci.yml:61` installs `php-cli php-sqlite3 php-xml php-mbstring php-pcov composer` for the `phpunit` job; `.github/workflows/deploy.yml:46` installs `php-cli` with no version. Nothing selects a PHP version, and a grep for `setup-php`, `php-version`, `.php-version` and `ovhconfig` found no pin.
- Consequence: `php -l`, the static render (`php www/index.php > …`), the lang-parity script and `composer install` all run on a PHP version chosen by the runner image, not by the project. A runner upgrade can change the PHP version and turn `main` red, or let syntax that the production host does not support pass the gate. This is the same class of unpinned-toolchain drift that ticket 8235 addressed for `npx html-validate`.

**Proposed change (repo-local):**
1. Operator confirms the PHP version the OVH shared-hosting plan actually runs (OVH control panel, PHP version for the `robotsix.net` hosting). Do not guess it.
2. Add the constraint to `composer.json` so Composer enforces it: `"require": { "php": "^X.Y" }` and `"config": { "platform": { "php": "X.Y.0" } }`. The `platform` setting makes `composer install` resolve dependencies against the production PHP version even on a newer runner. Canonical reference: https://getcomposer.org/doc/06-config.md#platform (not verified in this run; the web lookup tool failed).
3. Pin the apt PHP package in `ci.yml` (both jobs) and `deploy.yml` to the same minor version, e.g. `php8.X-cli`, or install via `shivammathur/setup-php@<sha>` with `php-version: 'X.Y'`. Keep it in sync with step 2.
4. Add a single line to `AGENT.md` naming the target PHP version and where it is enforced, so the constraint is discoverable.

**Acceptance:** `composer validate` passes; `composer install` fails fast on a PHP version outside the declared range; both workflows print the same PHP minor version in their install step.

**Why not fleet-wide:** the standards checkout was unreachable this run, so I could not confirm whether robotsix-standards already defines a PHP runtime convention. If it does, this ticket should adopt it rather than add a local variant.

Observed on: robotsix-website

<!-- survey-gap-id: php-runtime-version-undeclared -->
"""


@pytest.mark.parametrize(
    ("draft", "expected_line"),
    [
        (_BB70_DRAFT, "Repo owner enables"),
        (_A4E7_DRAFT, "Operator confirms the PHP version"),
    ],
    ids=["bb70-pvr-toggle", "a4e7-operator-php-version"],
)
def test_operator_prerequisite_draft_routes_to_human_gate(
    ctx_factory, monkeypatch, tmp_path, draft, expected_line
):
    """A survey draft that needs an operator action or operator-supplied
    fact is NOT auto-approved by the fast-path, even when auto-approve
    would say APPROVE — it lands in HUMAN_ISSUE_APPROVAL with the matched
    lines quoted."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="survey")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )
    auto_approve_calls: list[dict] = []

    def _spy_auto_approve(**kw):
        auto_approve_calls.append(kw)
        return refining.AutoApproveResult(decision="APPROVE", reason="ok")

    monkeypatch.setattr(refining, "triage_auto_approve", _spy_auto_approve)

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    assert out.next_state == State.HUMAN_ISSUE_APPROVAL
    assert "operator prerequisite detected" in out.note
    assert expected_line in out.note
    assert auto_approve_calls == []
    assert calls == []


def test_needs_approval_citing_sensitive_path_cannot_become_approve(
    ctx_factory, monkeypatch, tmp_path
):
    """a4e7 sub-defect: the fast-path classifier says NEEDS_APPROVAL and
    cites `.github/workflows/*.yml`; the later auto-approve pass in
    _resolve_next_state must not downgrade it to APPROVE."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="survey")
    _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )
    verdicts = iter(
        [
            refining.AutoApproveResult(
                decision="NEEDS_APPROVAL",
                reason=(
                    "The change modifies CI and deployment pipelines.\n"
                    "- `composer.json`\n"
                    "- `.github/workflows/ci.yml` (PHP installation)\n"
                    "- `.github/workflows/deploy.yml` (PHP installation)"
                ),
            ),
        ]
    )
    approve = refining.AutoApproveResult(decision="APPROVE", reason="repo-local")
    monkeypatch.setattr(
        refining, "triage_auto_approve", lambda **kw: next(verdicts, approve)
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Pin the PHP version in `composer.json` and both CI workflows.",
    )

    assert out.next_state == State.HUMAN_ISSUE_APPROVAL
    assert "sensitive path cited" in out.note
    assert ".github/workflows/ci.yml" in out.note
    assert "auto-approve: APPROVE" not in out.note


def test_plain_draft_keeps_fast_path_behaviour(ctx_factory, monkeypatch, tmp_path):
    """No operator markers, no sensitive path → fast-path unchanged."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="survey")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **kw: refining.AutoApproveResult(
            decision="APPROVE", reason="Mechanical rename"
        ),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Rename `old_func` to `new_func` in `src/foo/bar.py`.",
    )

    assert out.next_state == State.READY
    assert out.note.startswith("mechanical draft fast-path")
    assert "operator prerequisite" not in out.note
    assert calls == []


# ===========================================================================
# _no_change_path — external-fix claim re-verification branch
# ===========================================================================


def test_no_change_external_fix_claim_routes_to_implement(
    ctx_factory, monkeypatch, tmp_path
):
    ctx = ctx_factory()
    t = _ticket(ctx)  # no branch
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                no_change_needed=True,
                no_change_rationale=(
                    "The fix was **already shipped** in commit abc1234; "
                    "nothing to change."
                ),
            )
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert out.next_state is not State.DONE
    assert "unverified 'already implemented' claim routed to implement" in out.note
    desc = ctx.service.workspace(t).description_path.read_text(encoding="utf-8")
    assert "re-verify before closing" in desc
    assert "## Acceptance criteria" in desc


# ===========================================================================
# _short_circuit_for_internal_failure
# ===========================================================================


def test_short_circuit_pytest_failure_returns_outcome(
    ctx_factory, monkeypatch, tmp_path
):
    """A draft containing a pytest failure output short-circuits refine:
    the method returns an Outcome (not None), the full refine agent is NOT
    invoked, draft-original.md and an empty file_map.json are written,
    complexity is set to 'simple', and the spec contains the failure excerpt."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(monkeypatch)

    draft = (
        "CI run failed.\n\n"
        "============================= FAILURES =============================\n"
        "FAILED tests/test_x.py::test_foo - AssertionError: expected 1 got 2\n"
        "========================= short test summary ========================\n"
        "FAILED tests/test_x.py::test_foo\n"
    )

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    # Full refine agent never invoked.
    assert calls == []

    # An Outcome is returned, not None.
    assert out is not None
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert out.note.startswith("short-circuited refine")

    # Artifacts written.
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()
    assert (ws.artifacts_dir / "file_map.json").exists()
    file_map = json.loads(
        (ws.artifacts_dir / "file_map.json").read_text(encoding="utf-8")
    )
    assert file_map == []

    # Complexity recorded "simple".
    complexity_path = ws.artifacts_dir / "triage_complexity.json"
    assert complexity_path.exists()
    complexity_data = json.loads(complexity_path.read_text(encoding="utf-8"))
    assert complexity_data["complexity"] == "simple"

    # The spec markdown contains the failure excerpt.
    spec = ws.description_path.read_text(encoding="utf-8")
    assert "FAILURES" in spec
    assert "test_foo" in spec
    assert "internal toolchain failure" in spec.lower()


def test_short_circuit_mypy_failure_returns_outcome(ctx_factory, monkeypatch, tmp_path):
    """A draft containing a mypy error short-circuits refine."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(monkeypatch)

    draft = (
        "mypy run failed.\n\n"
        'src/foo.py:12: error: Argument 1 to "bar" has incompatible type '
        '"int"; expected "str"  [arg-type]\n'
    )

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    assert calls == []
    assert out is not None
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert out.note.startswith("short-circuited refine")

    ws = ctx.service.workspace(t)
    spec = ws.description_path.read_text(encoding="utf-8")
    assert "[arg-type]" in spec


def test_short_circuit_ruff_failure_returns_outcome(ctx_factory, monkeypatch, tmp_path):
    """A draft containing ruff failure output short-circuits refine."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(monkeypatch)

    draft = "ruff check failed.\n\nF401 imported but unused\n"

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    assert calls == []
    assert out is not None
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert out.note.startswith("short-circuited refine")


def test_short_circuit_no_failure_markers_falls_through(
    ctx_factory, monkeypatch, tmp_path
):
    """When the draft has no internal toolchain failure markers, the
    short-circuit returns None and the full refine agent runs."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(monkeypatch)

    draft = "We should add a new feature to the widget loader. It should handle edge cases better."

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    # Full refine agent ran.
    assert len(calls) == 1
    assert out.note.startswith("refined")


def test_short_circuit_reviewer_comments_falls_through(
    ctx_factory, monkeypatch, tmp_path
):
    """When reviewer comments are present, the short-circuit is skipped
    even when the draft contains failure markers — human-flagged changes
    always get full refinement."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ctx.service.add_comment(
        t.id, "Please also handle the edge case with None input.", author="user"
    )
    calls = _spy_refine(monkeypatch)

    draft = "CI run failed.\n\nFAILED tests/test_x.py::test_foo - AssertionError\n"

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    # Full refine agent ran despite the failure markers — reviewer comments
    # take priority.
    assert len(calls) == 1
    assert out.note.startswith("refined")


def test_short_circuit_empty_draft_falls_through(ctx_factory, monkeypatch, tmp_path):
    """An empty draft (or whitespace-only) does not short-circuit."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(monkeypatch)

    _run_agent(ctx, t, tmp_path, draft="   ")

    # Full refine agent ran.
    assert len(calls) == 1


def test_short_circuit_routes_to_implement_not_done(ctx_factory, monkeypatch, tmp_path):
    """The short-circuited outcome MUST route toward implement
    (READY / HUMAN_ISSUE_APPROVAL), never to DONE or CLOSED."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    _spy_refine(monkeypatch)

    draft = "CI run failed.\n\nFAILED tests/test_x.py::test_foo - AssertionError\n"

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    assert out.next_state not in (State.DONE, State.CLOSED)
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)


def test_short_circuit_with_evidence_file(ctx_factory, monkeypatch, tmp_path):
    """When ws.artifacts_dir / 'evidence.txt' exists, its content is
    embedded in the generated spec."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    _spy_refine(monkeypatch)

    ws = ctx.service.workspace(t)
    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (ws.artifacts_dir / "evidence.txt").write_text(
        "Traceback (most recent call last):\n  File ...\nRuntimeError: boom\n",
        encoding="utf-8",
    )

    draft = "CI run failed.\n\nFAILED tests/test_x.py::test_foo - AssertionError\n"

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    assert out is not None
    ws = ctx.service.workspace(t)
    spec = ws.description_path.read_text(encoding="utf-8")
    assert "evidence.txt" in spec
    assert "RuntimeError: boom" in spec


def test_short_circuit_static_method_direct_call(ctx_factory, tmp_path):
    """Drive _short_circuit_for_internal_failure directly (not through
    _run_refine_agent) to test the gating logic in isolation."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Case 1: Internal failure draft, no reviewer comments → Outcome returned.
    draft = "FAILED tests/test_x.py::test_foo - AssertionError\n"
    outcome = RefineStage._short_circuit_for_internal_failure(
        ctx, t, draft, ws, ctx.settings, reviewer_comments=None
    )
    assert outcome is not None
    assert outcome.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)

    # Case 2: Reviewer comments present → None returned.
    outcome2 = RefineStage._short_circuit_for_internal_failure(
        ctx, t, draft, ws, ctx.settings, reviewer_comments="Please fix scope."
    )
    assert outcome2 is None

    # Case 3: No failure markers → None returned.
    outcome3 = RefineStage._short_circuit_for_internal_failure(
        ctx,
        t,
        "Add a new feature to the loader.",
        ws,
        ctx.settings,
        reviewer_comments=None,
    )
    assert outcome3 is None

    # Case 4: Empty draft → None returned.
    outcome4 = RefineStage._short_circuit_for_internal_failure(
        ctx, t, "", ws, ctx.settings, reviewer_comments=None
    )
    assert outcome4 is None
