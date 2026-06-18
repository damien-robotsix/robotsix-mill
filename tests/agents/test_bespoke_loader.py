"""Tests for the bespoke-agent YAML loader.

Bespoke agents are operator-authored YAMLs committed to a managed
repo's source tree at ``<clone>/.robotsix-mill/agents/<name>.yaml``.
The loader is the trust boundary — anything operator-authored that
hits mill goes through it — so its failure-mode contract is strict:

- Malformed YAMLs MUST be skipped with a log warning, never raised.
  A typo in a committed managed-repo YAML must not be able to crash
  mill.
- Invalid names MUST be rejected so the operator's slug can never
  break ``source: bespoke:<name>`` or the memory filename.
- Duplicate names MUST be skipped (first write wins) so two YAMLs
  with the same name cannot scramble each other's memory or dedup.
"""

from __future__ import annotations

import logging

import pytest
import yaml

from robotsix_mill.agents.bespoke_loader import (
    BespokeAgentDefinition,
    load_bespoke_definitions,
)


# ---------------------------------------------------------------------------
#  Schema validation
# ---------------------------------------------------------------------------


class TestBespokeAgentDefinition:
    def _minimal_dict(self, **overrides):
        base = {
            "name": "mail-deliverability",
            "interval_seconds": 86400,
            "system_prompt": "You are a checker.",
        }
        base.update(overrides)
        return base

    def test_minimal_definition_parses(self):
        """Only name + interval_seconds + system_prompt are required.
        Everything else has sensible defaults so an operator's first
        bespoke YAML is a three-field starter file."""
        d = BespokeAgentDefinition.model_validate(self._minimal_dict())
        assert d.name == "mail-deliverability"
        assert d.interval_seconds == 86400
        assert d.system_prompt == "You are a checker."
        # Defaults the operator did not specify.
        assert d.description == ""
        assert d.level == 1  # default cheap tier
        assert d.web_knowledge is True  # ask_web_knowledge enabled by default

    def test_name_rejects_uppercase(self):
        with pytest.raises(ValueError, match="must match"):
            BespokeAgentDefinition.model_validate(
                self._minimal_dict(name="MailCheck"),
            )

    def test_name_rejects_path_traversal(self):
        """Names land in filesystem paths (memory ledger filename) and
        in the source string. A ``..`` or ``/`` would let an operator
        write outside ``<data_dir>/<board>/`` or scramble dedup."""
        for bad in ("..", "../escape", "a/b", "foo:bar", "x.y"):
            with pytest.raises(ValueError, match="must match"):
                BespokeAgentDefinition.model_validate(
                    self._minimal_dict(name=bad),
                )

    def test_name_rejects_leading_digit_or_hyphen(self):
        for bad in ("1foo", "-foo"):
            with pytest.raises(ValueError, match="must match"):
                BespokeAgentDefinition.model_validate(
                    self._minimal_dict(name=bad),
                )

    def test_name_accepts_valid_kebab_case(self):
        for ok in ("foo", "foo-bar", "a", "mail-deliverability-check"):
            d = BespokeAgentDefinition.model_validate(
                self._minimal_dict(name=ok),
            )
            assert d.name == ok

    def test_interval_seconds_floor_is_60(self):
        """An operator who drops a tiny interval would hammer the LLM
        on every cycle. Validation rejects anything under 60s — the
        operator must opt-in explicitly to a faster cadence by raising
        the value, not by accident."""
        with pytest.raises(ValueError):
            BespokeAgentDefinition.model_validate(
                self._minimal_dict(interval_seconds=10),
            )


# ---------------------------------------------------------------------------
#  Directory loading
# ---------------------------------------------------------------------------


class TestLoadBespokeDefinitions:
    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data))

    def test_returns_empty_when_repo_dir_is_none(self):
        """A periodic agent that runs without a clone (rare but
        possible) MUST get an empty list, never an exception."""
        assert load_bespoke_definitions(None) == []

    def test_returns_empty_when_no_robotsix_mill_folder(self, tmp_path):
        """A managed repo without a ``.robotsix-mill/`` folder ships
        no bespoke agents — same behaviour as before this feature."""
        assert load_bespoke_definitions(tmp_path) == []

    def test_returns_empty_when_agents_dir_missing(self, tmp_path):
        """``.robotsix-mill/`` exists (perhaps for overlays only) but
        no ``agents/`` subdir. Still a clean empty list."""
        (tmp_path / ".robotsix-mill" / "agent_overlays").mkdir(parents=True)
        assert load_bespoke_definitions(tmp_path) == []

    def test_loads_valid_definitions_sorted(self, tmp_path):
        """Multiple valid YAMLs are returned in alphabetical filename
        order so scheduling is deterministic across mill restarts."""
        d = tmp_path / ".robotsix-mill" / "agents"
        d.mkdir(parents=True)
        self._write(
            d / "zoo.yaml",
            {
                "name": "zoo",
                "interval_seconds": 3600,
                "system_prompt": "Z prompt",
            },
        )
        self._write(
            d / "alpha.yaml",
            {
                "name": "alpha",
                "interval_seconds": 7200,
                "system_prompt": "A prompt",
            },
        )
        defs = load_bespoke_definitions(tmp_path)
        assert [d.name for d in defs] == ["alpha", "zoo"]
        assert defs[0].interval_seconds == 7200
        assert defs[1].interval_seconds == 3600

    def test_skips_malformed_yaml(self, tmp_path, caplog):
        """A YAML parse error in one file does NOT take down the
        rest of the bespoke surface. The bad file is logged at
        WARNING and the good file still loads — a fat-finger commit
        on the managed repo can't disable every checker."""
        d = tmp_path / ".robotsix-mill" / "agents"
        d.mkdir(parents=True)
        (d / "broken.yaml").write_text("not: valid: yaml: at: all")
        self._write(
            d / "good.yaml",
            {
                "name": "good",
                "interval_seconds": 3600,
                "system_prompt": "P",
            },
        )
        with caplog.at_level(logging.WARNING):
            defs = load_bespoke_definitions(tmp_path)
        assert [d.name for d in defs] == ["good"]
        assert any("broken.yaml" in r.message for r in caplog.records)

    def test_skips_schema_errors(self, tmp_path, caplog):
        """A YAML that parses but fails the BespokeAgentDefinition
        contract (invalid name, missing prompt, tiny interval) is
        skipped at WARNING. Other files still load."""
        d = tmp_path / ".robotsix-mill" / "agents"
        d.mkdir(parents=True)
        # Missing required system_prompt.
        self._write(
            d / "no-prompt.yaml",
            {
                "name": "no-prompt",
                "interval_seconds": 3600,
            },
        )
        # Invalid name (uppercase).
        self._write(
            d / "bad-name.yaml",
            {
                "name": "BadName",
                "interval_seconds": 3600,
                "system_prompt": "P",
            },
        )
        self._write(
            d / "good.yaml",
            {
                "name": "good",
                "interval_seconds": 3600,
                "system_prompt": "P",
            },
        )
        with caplog.at_level(logging.WARNING):
            defs = load_bespoke_definitions(tmp_path)
        assert [d.name for d in defs] == ["good"]

    def test_duplicate_names_first_wins(self, tmp_path, caplog):
        """Two files declaring the same ``name`` would collide in
        ``source: bespoke:<name>`` and the memory filename. The
        loader takes the first one (alphabetical file order) and
        warns about the rest — silent collision would be worse."""
        d = tmp_path / ".robotsix-mill" / "agents"
        d.mkdir(parents=True)
        self._write(
            d / "a.yaml",
            {
                "name": "shared",
                "interval_seconds": 3600,
                "system_prompt": "First wins",
            },
        )
        self._write(
            d / "b.yaml",
            {
                "name": "shared",
                "interval_seconds": 7200,
                "system_prompt": "Second skipped",
            },
        )
        with caplog.at_level(logging.WARNING):
            defs = load_bespoke_definitions(tmp_path)
        assert len(defs) == 1
        assert defs[0].system_prompt == "First wins"
        assert any(
            "duplicate name" in r.message and "shared" in r.message
            for r in caplog.records
        )

    def test_top_level_must_be_mapping(self, tmp_path, caplog):
        """A YAML whose top level is a list / scalar / null is a clear
        operator error — skip with warning."""
        d = tmp_path / ".robotsix-mill" / "agents"
        d.mkdir(parents=True)
        (d / "list.yaml").write_text("- foo\n- bar\n")
        (d / "null.yaml").write_text("")
        self._write(
            d / "good.yaml",
            {
                "name": "good",
                "interval_seconds": 3600,
                "system_prompt": "P",
            },
        )
        with caplog.at_level(logging.WARNING):
            defs = load_bespoke_definitions(tmp_path)
        assert [d.name for d in defs] == ["good"]
