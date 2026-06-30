"""Tests for per-repo periodic-workflow discovery + override resolution."""

from __future__ import annotations

from pathlib import Path

from robotsix_mill.agents import periodic_loader as pl


def _write(repo: Path, name: str, body: str) -> Path:
    d = repo / ".robotsix-mill" / "periodic"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# --- kind map ---------------------------------------------------------------


def test_kind_for_classification():
    assert pl.kind_for("audit") == "llm_agent"
    assert pl.kind_for("trace_review") == "schedule_only"
    assert pl.kind_for("langfuse_cleanup") == "global_only"
    assert pl.kind_for("meta") == "global_only"
    assert pl.kind_for("my-custom-thing") == "bespoke"


# --- llm_agent partial-merge over a real built-in (audit) -------------------


def test_name_only_inherits_builtin(tmp_path):
    p = _write(tmp_path, "audit", "name: audit\n")
    r = pl.resolve_periodic_workflow(p)
    assert r is not None and r.kind == "llm_agent" and r.enabled is True
    # inherits the shipped prompt + model
    from robotsix_mill.agents.yaml_loader import load_agent_definition

    builtin = load_agent_definition(Path("agent_definitions/periodic/audit.yaml"))
    assert r.definition.system_prompt == builtin.system_prompt
    assert r.definition.name == "audit"


def test_field_override_merges(tmp_path):
    p = _write(tmp_path, "audit", "name: audit\nlevel: 1\nretries: 7\n")
    r = pl.resolve_periodic_workflow(p)
    # Level resolution to (transport, model) happens in build_agent, not here —
    # the loader stores the literal level. The override fields merge over the
    # builtin.
    assert r.definition.level == 1
    assert r.definition.retries == 7
    # untouched fields inherit from the builtin
    assert r.definition.name == "audit"
    assert r.definition.output_type  # audit has a structured output_type


def test_prompt_overlay_appends(tmp_path):
    p = _write(
        tmp_path,
        "audit",
        "name: audit\nprompt_overlay: |\n  EXTRA REPO RULE: foo.\n",
    )
    r = pl.resolve_periodic_workflow(p)
    from robotsix_mill.agents.yaml_loader import load_agent_definition

    builtin = load_agent_definition(Path("agent_definitions/periodic/audit.yaml"))
    assert r.definition.system_prompt.startswith(builtin.system_prompt)
    assert "EXTRA REPO RULE: foo." in r.definition.system_prompt


def test_system_prompt_replaces(tmp_path):
    p = _write(tmp_path, "audit", "name: audit\nsystem_prompt: REPLACED.\n")
    r = pl.resolve_periodic_workflow(p)
    assert r.definition.system_prompt == "REPLACED."


def test_prompt_overlay_and_system_prompt_mutually_exclusive(tmp_path):
    p = _write(
        tmp_path,
        "audit",
        "name: audit\nsystem_prompt: a\nprompt_overlay: b\n",
    )
    # xor validation error → skipped (None), not raised
    assert pl.resolve_periodic_workflow(p) is None


def test_enabled_false_is_respected(tmp_path):
    p = _write(tmp_path, "audit", "name: audit\nenabled: false\n")
    r = pl.resolve_periodic_workflow(p)
    assert r is not None and r.enabled is False


def test_interval_override_carried(tmp_path):
    p = _write(tmp_path, "audit", "name: audit\ninterval_seconds: 3600\n")
    r = pl.resolve_periodic_workflow(p)
    assert r.interval_seconds == 3600


def test_human_readable_interval_override_flows_through(tmp_path):
    """A per-repo ``interval: 2d`` override is parsed to seconds AND
    survives the exclude_unset merge → ResolvedPeriodicWorkflow gets it."""
    p = _write(tmp_path, "audit", "name: audit\ninterval: 2d\n")
    r = pl.resolve_periodic_workflow(p)
    assert r is not None
    assert r.interval_seconds == 172800
    # The merged AgentDefinition also carries the backfilled seconds and
    # not a conflicting raw ``interval``.
    assert r.definition.interval_seconds == 172800


def test_interval_and_interval_seconds_both_set_skipped(tmp_path):
    """Both forms set → schema error → file logged-and-skipped (None)."""
    p = _write(tmp_path, "audit", "name: audit\ninterval: 2d\ninterval_seconds: 10\n")
    assert pl.resolve_periodic_workflow(p) is None


def test_malformed_interval_skipped(tmp_path):
    """A malformed ``interval`` string → logged-and-skipped (never raises)."""
    p = _write(tmp_path, "audit", "name: audit\ninterval: 1x\n")
    assert pl.resolve_periodic_workflow(p) is None


# --- schedule_only / maintenance: no prompt ---------------------------------


def test_schedule_only_has_no_definition(tmp_path):
    p = _write(tmp_path, "trace_review", "name: trace_review\ninterval_seconds: 7200\n")
    r = pl.resolve_periodic_workflow(p)
    assert r.kind == "schedule_only" and r.definition is None
    assert r.interval_seconds == 7200


def test_global_only_langfuse_cleanup_ignored(tmp_path):
    p = _write(tmp_path, "langfuse_cleanup", "name: langfuse_cleanup\n")
    assert pl.resolve_periodic_workflow(p) is None


# --- bespoke (unmatched name) ----------------------------------------------


def test_bespoke_requires_system_prompt(tmp_path):
    p = _write(tmp_path, "my-thing", "name: my-thing\ninterval_seconds: 86400\n")
    # no system_prompt → skipped
    assert pl.resolve_periodic_workflow(p) is None


def test_bespoke_builds_definition(tmp_path):
    p = _write(
        tmp_path,
        "my-thing",
        "name: my-thing\nsystem_prompt: do the thing\nlevel: 2\n",
    )
    r = pl.resolve_periodic_workflow(p)
    assert r is not None and r.kind == "bespoke"
    assert r.definition.system_prompt == "do the thing"
    assert r.definition.level == 2


def test_bespoke_empty_level_defaults_to_one(tmp_path):
    p = _write(tmp_path, "my-thing", "name: my-thing\nsystem_prompt: x\n")
    r = pl.resolve_periodic_workflow(p)
    # A brand-new bespoke periodic agent with no level declared defaults to
    # level 1 (cheap tier).
    assert r.definition.level == 1


# --- global_only ignored ----------------------------------------------------


def test_global_only_name_ignored(tmp_path):
    p = _write(tmp_path, "meta", "name: meta\nsystem_prompt: x\n")
    assert pl.resolve_periodic_workflow(p) is None


# --- malformed handling -----------------------------------------------------


def test_malformed_yaml_skipped(tmp_path):
    p = _write(tmp_path, "audit", "name: audit\n  : bad: [\n")
    assert pl.resolve_periodic_workflow(p) is None


def test_unknown_key_rejected(tmp_path):
    p = _write(tmp_path, "audit", "name: audit\nbogus_key: 1\n")
    assert pl.resolve_periodic_workflow(p) is None


# --- discovery --------------------------------------------------------------


def test_discover_none_repo():
    assert pl.discover_periodic_workflows(None) == []


def test_discover_absent_dir(tmp_path):
    assert pl.discover_periodic_workflows(tmp_path) == []


def test_discover_classifies_and_collects(tmp_path):
    _write(tmp_path, "audit", "name: audit\n")
    _write(tmp_path, "langfuse_cleanup", "name: langfuse_cleanup\n")
    _write(tmp_path, "my-thing", "name: my-thing\nsystem_prompt: x\n")
    _write(tmp_path, "meta", "name: meta\nsystem_prompt: x\n")  # ignored
    _write(tmp_path, "broken", "name: broken\n")  # bespoke w/o prompt → skipped
    out = {w.name: w.kind for w in pl.discover_periodic_workflows(tmp_path)}
    assert out == {
        "audit": "llm_agent",
        "my-thing": "bespoke",
    }


def test_discover_name_defaults_to_stem(tmp_path):
    # file with no explicit name field → name taken from filename stem
    _write(tmp_path, "health", "interval_seconds: 600\n")
    out = pl.discover_periodic_workflows(tmp_path)
    assert len(out) == 1 and out[0].name == "health" and out[0].kind == "llm_agent"
    assert out[0].interval_seconds == 600


# --- validate_periodic_file_content ----------------------------------------


def test_validate_bespoke_name_without_prompt_rejected():
    errs = pl.validate_periodic_file_content("board_cleanup", None)
    assert len(errs) > 0
    assert "board_cleanup" in errs[0]
    # Must list valid built-in names
    assert "audit" in errs[0] or any("audit" in e for e in errs)


def test_validate_known_builtin_name_only_is_valid():
    errs = pl.validate_periodic_file_content("audit", None)
    assert errs == []


def test_validate_bespoke_with_prompt_is_valid():
    errs = pl.validate_periodic_file_content("my_bespoke", "Do something useful.")
    assert errs == []


def test_validate_global_only_rejected():
    errs = pl.validate_periodic_file_content("langfuse_cleanup", None)
    assert len(errs) > 0
    assert "global" in errs[0].lower() or "cross-repo" in errs[0].lower()


def test_validate_bespoke_blank_prompt_rejected():
    errs = pl.validate_periodic_file_content("unknown_thing", "   \n  ")
    assert len(errs) > 0
    assert "unknown_thing" in errs[0]


def test_validate_known_schedule_only_is_valid():
    errs = pl.validate_periodic_file_content("trace_review", None)
    assert errs == []
