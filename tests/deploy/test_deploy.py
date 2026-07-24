"""Unit tests for ``robotsix_mill.deploy`` — deploy-freshness checks
and config-standard footprint validation."""

from __future__ import annotations

import tempfile
from dataclasses import FrozenInstanceError
from pathlib import Path

import httpx
import pytest

from robotsix_mill.deploy import (
    DeployStatus,
    check_deploy_freshness,
    validate_config_standard_footprint,
)


# ---------------------------------------------------------------------------
# Fake httpx client helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    """A minimal fake httpx.Response for testing check_deploy_freshness."""

    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeClient:
    """A fake httpx.Client that returns a canned response for GET."""

    def __init__(self, response, **kw):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, url):
        return self._response


# ---------------------------------------------------------------------------
# check_deploy_freshness
# ---------------------------------------------------------------------------


def test_check_deploy_freshness_none_url_returns_none():
    """When deploy_api_url is None, the gate is disabled."""
    assert check_deploy_freshness(None) is None


def test_check_deploy_freshness_empty_url_returns_none():
    """When deploy_api_url is empty string, the gate is disabled."""
    assert check_deploy_freshness("") is None


def test_check_deploy_freshness_current_image(monkeypatch):
    """When running_digest == latest_digest, update_available is False."""
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _FakeClient(
            _FakeResponse(
                200,
                {
                    "running_digest": "sha256:abc123",
                    "latest_digest": "sha256:abc123",
                    "update_available": False,
                },
            )
        ),
    )
    status = check_deploy_freshness("http://deploy:8080")
    assert status is not None
    assert status.running_digest == "sha256:abc123"
    assert status.latest_digest == "sha256:abc123"
    assert status.update_available is False


def test_check_deploy_freshness_stale_image(monkeypatch):
    """When running_digest != latest_digest, update_available is True."""
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _FakeClient(
            _FakeResponse(
                200,
                {
                    "running_digest": "sha256:old",
                    "latest_digest": "sha256:new",
                    "update_available": True,
                },
            )
        ),
    )
    status = check_deploy_freshness("http://deploy:8080")
    assert status is not None
    assert status.running_digest == "sha256:old"
    assert status.latest_digest == "sha256:new"
    assert status.update_available is True


def test_check_deploy_freshness_update_available_inferred(monkeypatch):
    """When update_available key is missing, it's inferred from digest mismatch."""
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _FakeClient(
            _FakeResponse(
                200,
                {
                    "running_digest": "sha256:old",
                    "latest_digest": "sha256:new",
                },
            )
        ),
    )
    status = check_deploy_freshness("http://deploy:8080")
    assert status is not None
    assert status.update_available is True


def test_check_deploy_freshness_server_unreachable_returns_none(monkeypatch):
    """When the deploy server returns 500, return None (don't block)."""
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _FakeClient(_FakeResponse(500)),
    )
    status = check_deploy_freshness("http://deploy:8080")
    assert status is None


def test_check_deploy_freshness_unexpected_payload_returns_none(monkeypatch):
    """When the response is missing required keys, return None."""
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _FakeClient(_FakeResponse(200, {"unexpected": "payload"})),
    )
    status = check_deploy_freshness("http://deploy:8080")
    assert status is None


def test_check_deploy_freshness_connection_error_returns_none(monkeypatch):
    """When the deploy server cannot be reached, return None."""

    class _ErrorClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "Client", _ErrorClient)
    status = check_deploy_freshness("http://deploy:8080")
    assert status is None


def test_check_deploy_freshness_normalizes_missing_scheme(monkeypatch):
    """When deploy_api_url lacks a scheme, https:// is prepended automatically."""
    captured_url: list[str] = []

    class _CaptureClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            captured_url.append(url)
            return _FakeResponse(
                200,
                {
                    "running_digest": "sha256:abc",
                    "latest_digest": "sha256:abc",
                    "update_available": False,
                },
            )

    monkeypatch.setattr(httpx, "Client", _CaptureClient)
    status = check_deploy_freshness("deploy.example.com:8080")
    assert status is not None
    assert captured_url == ["https://deploy.example.com:8080/services/mill"]


# ---------------------------------------------------------------------------
# DeployStatus dataclass
# ---------------------------------------------------------------------------


def test_deploy_status_immutable():
    """DeployStatus is frozen."""
    status = DeployStatus(
        running_digest="sha256:a", latest_digest="sha256:b", update_available=True
    )
    with pytest.raises(FrozenInstanceError):
        status.running_digest = "changed"  # type: ignore[misc]


def test_deploy_status_equality():
    """DeployStatus supports equality comparison."""
    a = DeployStatus(running_digest="a", latest_digest="b", update_available=True)
    b = DeployStatus(running_digest="a", latest_digest="b", update_available=True)
    c = DeployStatus(running_digest="a", latest_digest="a", update_available=False)
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# validate_config_standard_footprint — simple tests (no diff filtering)
# ---------------------------------------------------------------------------


def test_footprint_clean_repo_with_ordinary_yaml(tmp_path):
    """Ordinary repo yaml files are NOT footprint violations.

    Regression: an earlier implementation globbed every ``*.yaml``/``*.yml``
    and flagged any file outside the four-file footprint, which blocked
    virtually every repo (they all carry unrelated yaml) fleet-wide.
    """
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.yaml").write_text("x: 1\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
    (tmp_path / "mkdocs.yml").write_text("site_name: x\n")

    assert validate_config_standard_footprint(tmp_path) == []


def test_footprint_flags_stray_standards_dir(tmp_path):
    """A stray ``_standards/`` copy IS flagged as a violation."""
    (tmp_path / "_standards").mkdir()
    (tmp_path / "_standards" / "contract.md").write_text("# standards\n")

    violations = validate_config_standard_footprint(tmp_path)
    assert "_standards" in violations


def test_footprint_empty_repo(tmp_path):
    """An empty repo has a clean footprint."""
    assert validate_config_standard_footprint(tmp_path) == []


# ---------------------------------------------------------------------------
# validate_config_standard_footprint — diff-filtering tests
# ---------------------------------------------------------------------------


def _make_repo(base: Path, files: dict[str, str]) -> Path:
    """Create a minimal repo directory tree from a dict of path→content."""
    base.mkdir(parents=True, exist_ok=True)
    for relpath, content in files.items():
        full = base / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return base


class TestValidateFootprintNoDiffFilter:
    """Backward-compatible: no diff_files → all violations reported."""

    def test_clean_repo(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(
                Path(td),
                {
                    "config/config.json": "{}",
                    "CHANGELOG.md": "# Changelog",
                },
            )
            assert validate_config_standard_footprint(repo) == []

    def test_stray_standards_dir(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(
                Path(td),
                {
                    "_standards/something.yaml": "foo: bar",
                },
            )
            violations = validate_config_standard_footprint(repo)
            assert "_standards" in violations


class TestValidateFootprintWithDiffFilter:
    """diff_files provided → only diff-touched violations reported."""

    def test_pre_existing_stray_dir_not_flagged(self):
        """A _standards/ dir that exists but is NOT in the diff passes."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(
                Path(td),
                {
                    "_standards/something.yaml": "foo: bar",
                },
            )
            # Diff touches only an unrelated file.
            diff = {"CHANGELOG.md"}
            assert validate_config_standard_footprint(repo, diff_files=diff) == []

    def test_diff_touched_stray_dir_flagged(self):
        """A _standards/ dir whose file IS in the diff is flagged."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(
                Path(td),
                {
                    "_standards/something.yaml": "foo: bar",
                },
            )
            diff = {"_standards/something.yaml"}
            violations = validate_config_standard_footprint(repo, diff_files=diff)
            assert "_standards" in violations

    def test_pre_existing_fleet_files_not_flagged(self):
        """Pre-existing fleet-standard files (.pre-commit-config.yaml, etc.)
        are not flagged even when they live outside the footprint."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(
                Path(td),
                {
                    ".pre-commit-config.yaml": "repos: []",
                    "docker-compose.yml": "services:",
                    "mkdocs.yml": "site_name: Test",
                },
            )
            # Diff touches only an unrelated file.
            diff = {"CHANGELOG.md"}
            violations = validate_config_standard_footprint(repo, diff_files=diff)
            assert violations == []
