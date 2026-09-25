"""Regression tests for scripts/render_harden_dockerfile.py.

The reusable Docker publish workflow
(``robotsix-github-workflows/.github/workflows/docker-release.yml``) invokes
``python3 scripts/render_harden_dockerfile.py`` against *this* repo's checkout
during the Release run.  The script must therefore be vendored here and behave
exactly as the workflow expects, so these tests exercise the same rendering /
user-extraction logic the workflow relies on.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.script_loader import load_script

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "render_harden_dockerfile.py"

_mod = load_script(_SCRIPT_PATH)

extract_user = _mod.extract_user
render = _mod.render
main = _mod.main


# ---------------------------------------------------------------------------
#  extract_user — tolerant of every inspect JSON shape
# ---------------------------------------------------------------------------


def test_extract_user_oci_image_config_nested() -> None:
    assert extract_user({"config": {"User": "app"}}) == "app"


def test_extract_user_docker_inspect_top_level() -> None:
    assert extract_user({"User": "1000:1000"}) == "1000:1000"


def test_extract_user_manifest_list_prefers_amd64() -> None:
    cfg = {
        "linux/arm64": {"config": {"User": "arm"}},
        "linux/amd64": {"config": {"User": "amd"}},
    }
    assert extract_user(cfg) == "amd"


def test_extract_user_missing_or_bad_input_is_empty() -> None:
    assert extract_user(None) == ""
    assert extract_user("nope") == ""
    assert extract_user({}) == ""
    assert extract_user({"config": {"User": ""}}) == ""


# ---------------------------------------------------------------------------
#  render — Dockerfile text
# ---------------------------------------------------------------------------


def test_render_root_base_omits_user_restore() -> None:
    out = render("ghcr.io/x/y@sha256:abc", user="")
    assert out.startswith("# Auto-generated")
    assert "FROM ghcr.io/x/y@sha256:abc" in out
    assert "USER root" in out
    assert "apt-get" in out
    # No USER restore line beyond the root switch.
    assert out.count("USER ") == 1


def test_render_non_root_base_restores_user() -> None:
    out = render("img@sha256:d", user="app")
    assert "USER root" in out
    assert out.rstrip().endswith("USER app")


def test_render_root_user_not_restored() -> None:
    assert "USER root\n" in render("img", user="root")
    assert render("img", user="0").count("USER ") == 1


def test_render_no_apt_omits_upgrade() -> None:
    out = render("img@sha256:d", user="app", apt=False)
    assert "apt-get" not in out
    assert out.rstrip().endswith("USER app")


def test_render_empty_base_ref_raises() -> None:
    try:
        render("   ")
    except ValueError:
        return
    raise AssertionError("expected ValueError for empty base_ref")


# ---------------------------------------------------------------------------
#  main — the exact CLI the workflow calls
# ---------------------------------------------------------------------------


def test_main_writes_out_file(tmp_path: Path) -> None:
    config = tmp_path / "base-config.json"
    config.write_text(json.dumps({"config": {"User": "app"}}), encoding="utf-8")
    out = tmp_path / "Dockerfile"

    rc = main(
        [
            "--base-ref",
            "img@sha256:deadbeef",
            "--config",
            str(config),
            "--out",
            str(out),
        ]
    )

    assert rc == 0
    rendered = out.read_text(encoding="utf-8")
    assert "FROM img@sha256:deadbeef" in rendered
    assert rendered.rstrip().endswith("USER app")


def test_main_tolerates_malformed_config(tmp_path: Path) -> None:
    config = tmp_path / "bad.json"
    config.write_text("not json", encoding="utf-8")
    out = tmp_path / "Dockerfile"

    rc = main(["--base-ref", "img", "--config", str(config), "--out", str(out)])

    assert rc == 0
    # Malformed config → run as root, no restore line.
    assert out.read_text(encoding="utf-8").count("USER ") == 1
