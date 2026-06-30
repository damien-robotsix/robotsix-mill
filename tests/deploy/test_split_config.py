"""Tests for the deploy-mode config splitter (``deploy/split_config.py``).

The script is a standalone entrypoint helper (not part of the ``robotsix_mill``
package), so it is loaded by file path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "split_config.py"
_spec = importlib.util.spec_from_file_location("_split_config", _SCRIPT)
assert _spec and _spec.loader
split_config_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(split_config_mod)


def _run(tmp_path: Path, doc: dict) -> tuple[dict, dict]:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(doc), encoding="utf-8")
    split_config_mod.split_config(str(cfg), str(tmp_path))
    secrets = yaml.safe_load((tmp_path / "secrets.yaml").read_text()) or {}
    overlay = yaml.safe_load((tmp_path / "mill.local.yaml").read_text()) or {}
    return secrets, overlay


def test_secret_sentinel_is_dropped(tmp_path: Path) -> None:
    """A leaf left at the literal "SECRET" sentinel must NOT be written to
    secrets.yaml — it would otherwise be ingested as a real credential."""
    secrets, _ = _run(
        tmp_path,
        {
            "secrets": {
                "openrouter_api_key": "sk-real",  # configured → kept
                "forge_token": "SECRET",  # unconfigured sentinel → dropped
                "langfuse_secret_key": "",  # blank → dropped
                "langfuse_public_key": None,  # null → dropped
            }
        },
    )
    assert secrets == {"openrouter_api_key": "sk-real"}
    assert "forge_token" not in secrets


def test_overlay_excludes_secrets_and_forces_invariants(tmp_path: Path) -> None:
    _, overlay = _run(
        tmp_path,
        {
            "secrets": {"forge_token": "SECRET"},
            "forge": {"kind": "github"},
            "gates": {"auto_merge_enabled": True},
        },
    )
    assert "secrets" not in overlay
    assert overlay["forge"] == {"kind": "github"}
    assert overlay["gates"] == {"auto_merge_enabled": True}
    # Deploy invariants are forced regardless of the form.
    assert overlay["service"]["api_host"] == "0.0.0.0"
    assert overlay["service"]["data_dir"] == "/data"
