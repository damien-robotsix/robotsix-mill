"""Tests for ``deploy/split_config.py`` — the central-deploy config splitter.

central-deploy writes a single operator form (``config/config.yaml``) into
the mill-config volume; ``split_config.py`` (run by ``entrypoint.sh`` in
deploy mode) splits it into the flat ``secrets.yaml`` + non-secret
``mill.local.yaml`` overlay the mill's loader actually reads.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import yaml

from tests.script_loader import load_script

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_split = load_script(_REPO_ROOT / "deploy" / "split_config.py", "split_config")


def _run(tmp_path: Path, doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(doc), encoding="utf-8")
    _split.split_config(str(cfg), str(tmp_path))
    secrets = yaml.safe_load((tmp_path / "secrets.yaml").read_text()) or {}
    overlay = yaml.safe_load((tmp_path / "mill.local.yaml").read_text()) or {}
    return secrets, overlay


def test_real_secret_values_kept(tmp_path: Path) -> None:
    secrets, _ = _run(
        tmp_path,
        {"secrets": {"openrouter_api_key": "sk-or-123", "forge_token": "ghp_x"}},
    )
    assert secrets == {"openrouter_api_key": "sk-or-123", "forge_token": "ghp_x"}


def test_blank_and_none_leaves_dropped(tmp_path: Path) -> None:
    secrets, _ = _run(
        tmp_path,
        {"secrets": {"a": "", "b": None, "c": "   ", "d": "keep"}},
    )
    assert secrets == {"d": "keep"}


def test_secret_sentinel_dropped(tmp_path: Path) -> None:
    """A residual central-deploy ``SECRET`` sentinel must never be ingested
    as a real credential — it is treated as blank."""
    secrets, _ = _run(
        tmp_path,
        {"secrets": {"openrouter_api_key": "SECRET", "forge_token": "real-token"}},
    )
    assert "openrouter_api_key" not in secrets
    assert secrets == {"forge_token": "real-token"}


def test_overlay_excludes_secrets_and_forces_invariants(tmp_path: Path) -> None:
    _, overlay = _run(
        tmp_path,
        {
            "secrets": {"forge_token": "x"},
            "forge": {"kind": "github"},
            "service": {"port": 8077},
        },
    )
    assert "secrets" not in overlay
    assert overlay["forge"] == {"kind": "github"}
    # Deploy invariants are forced regardless of the submitted form.
    assert overlay["service"]["api_host"] == "0.0.0.0"
    assert overlay["service"]["data_dir"] == "/data"
    assert overlay["service"]["port"] == 8077


def test_secrets_file_is_owner_only(tmp_path: Path) -> None:
    _run(tmp_path, {"secrets": {"forge_token": "x"}})
    mode = stat.S_IMODE((tmp_path / "secrets.yaml").stat().st_mode)
    assert mode == 0o600
