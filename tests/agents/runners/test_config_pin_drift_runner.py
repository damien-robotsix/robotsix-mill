"""Tests for the config pin-drift runner.

The failure this guards against: ``config/config.json`` pins a setting, the
code default later changes, and the pin silently wins — so the change is a
no-op in production. It reverted a move to weekly periodics (twelve generators
ran daily for weeks) and a change disabling the per-ticket spend caps, both
found by hand long after the fact.
"""

from __future__ import annotations

from types import SimpleNamespace

from robotsix_mill.agents.runners import config_pin_drift_runner as mod


def _patch_pins(monkeypatch, pins: dict) -> None:
    monkeypatch.setattr(mod, "_settings_pins", lambda: pins)


def _settings(baseline: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(config_pin_drift_baseline=baseline or [])


class TestDetectPinDrift:
    def test_pin_matching_the_default_is_not_drift(self, monkeypatch) -> None:
        from robotsix_mill.config import Settings

        default = Settings.model_fields["stage_retry_max_attempts"].default
        _patch_pins(monkeypatch, {"stage_retry_max_attempts": default})
        result = mod.detect_pin_drift(_settings())
        assert result.checked == 1
        assert result.drifted == []

    def test_pin_shadowing_a_changed_default_is_reported(self, monkeypatch) -> None:
        """The real case: the code default moved, the pin did not."""
        from robotsix_mill.config import Settings

        default = Settings.model_fields["stage_retry_max_attempts"].default
        _patch_pins(monkeypatch, {"stage_retry_max_attempts": default + 7})
        result = mod.detect_pin_drift(_settings())
        assert [d.key for d in result.drifted] == ["stage_retry_max_attempts"]
        drift = result.drifted[0]
        assert drift.pinned == default + 7
        assert drift.default == default
        # The message must name both values — "which one is live" is the
        # first thing an operator needs.
        assert str(default) in drift.describe()
        assert str(default + 7) in drift.describe()

    def test_baselined_key_is_not_reported(self, monkeypatch) -> None:
        """A deliberate operator choice is recorded once and stays quiet."""
        from robotsix_mill.config import Settings

        default = Settings.model_fields["stage_retry_max_attempts"].default
        _patch_pins(monkeypatch, {"stage_retry_max_attempts": default + 7})
        result = mod.detect_pin_drift(_settings(["stage_retry_max_attempts"]))
        assert result.drifted == []
        assert result.baselined == 1

    def test_unknown_key_is_skipped(self, monkeypatch) -> None:
        """A pin with no matching field is config-surface drift, which
        check_config_sync.py owns — not this pass."""
        _patch_pins(monkeypatch, {"not_a_real_setting": 123})
        result = mod.detect_pin_drift(_settings())
        assert result.checked == 0
        assert result.drifted == []

    def test_default_factory_fields_are_skipped(self, monkeypatch) -> None:
        """List-valued settings report PydanticUndefined as their default;
        comparing against it would flag every one of them on every pass."""
        _patch_pins(monkeypatch, {"auto_merge_infra_denylist": ["something-else"]})
        result = mod.detect_pin_drift(_settings())
        assert result.drifted == []

    def test_missing_config_file_is_not_drift(self, monkeypatch) -> None:
        """A deployment without a config file must not be reported as drift,
        and must not crash the pass."""
        _patch_pins(monkeypatch, {})
        result = mod.detect_pin_drift(_settings())
        assert result.checked == 0
        assert result.drifted == []

    def test_unreadable_config_degrades_quietly(self, monkeypatch) -> None:
        """A parse failure yields no pins rather than propagating — this pass
        must never be the thing that takes the worker down."""

        def boom() -> dict:
            raise OSError("unreadable")

        monkeypatch.setattr(
            "robotsix_mill.config.loader.load_settings_block", boom, raising=False
        )
        assert mod._settings_pins() == {}


class TestRunConfigPinDrift:
    def test_summary_reports_counts_and_keys(self, monkeypatch) -> None:
        from robotsix_mill.config import Settings

        default = Settings.model_fields["stage_retry_max_attempts"].default
        _patch_pins(monkeypatch, {"stage_retry_max_attempts": default + 7})
        out = mod.run_config_pin_drift(_settings())
        assert out["drifted"] == 1
        assert out["keys"] == ["stage_retry_max_attempts"]

    def test_clean_pass_reports_zero(self, monkeypatch) -> None:
        from robotsix_mill.config import Settings

        default = Settings.model_fields["stage_retry_max_attempts"].default
        _patch_pins(monkeypatch, {"stage_retry_max_attempts": default})
        out = mod.run_config_pin_drift(_settings())
        assert out["drifted"] == 0
        assert out["checked"] == 1

    def test_warning_names_every_drifting_key(self, monkeypatch, caplog) -> None:
        """The log line is the entire product of this pass — it must carry the
        keys, not just a count."""
        from robotsix_mill.config import Settings

        d1 = Settings.model_fields["stage_retry_max_attempts"].default
        d2 = Settings.model_fields["sandbox_op_timeout"].default
        _patch_pins(
            monkeypatch,
            {"stage_retry_max_attempts": d1 + 7, "sandbox_op_timeout": d2 + 7},
        )
        with caplog.at_level("WARNING"):
            mod.run_config_pin_drift(_settings())
        text = caplog.text
        assert "stage_retry_max_attempts" in text
        assert "sandbox_op_timeout" in text
