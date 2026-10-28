"""Tests for the read-only cross-repo ``read_fleet_file`` / ``grep_fleet_repo``
tools and their wiring into the review agent."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import robotsix_mill.agents.fleet_file_tools as fft
from robotsix_mill.config import Secrets, Settings, _reset_secrets

_DOCKER_RELEASE = """\
name: docker-release
permissions:
  id-token: write
  attestations: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: docker buildx build --provenance mode=max --sbom true .
"""


class _FakeForge:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self.reads: list[tuple[str, str]] = []

    def read_repo_file(self, *, path: str, ref: str = "") -> str | None:
        self.reads.append((path, ref))
        return self.files.get(path)

    def list_repo_files(self, *, ref: str = "") -> list[str] | None:
        return list(self.files)


@pytest.fixture
def fleet(monkeypatch):
    """Register one fleet repo backed by a fake (read-only) forge."""
    forge = _FakeForge({".github/workflows/docker-release.yml": _DOCKER_RELEASE})
    repos = {
        "robotsix-github-workflows": SimpleNamespace(
            forge_remote_url="https://github.com/o/robotsix-github-workflows"
        ),
        "no-remote": SimpleNamespace(forge_remote_url=None),
    }
    monkeypatch.setattr(fft, "_registered_repos", lambda: repos)
    built: list = []

    def fake_get_forge(settings, repo_config=None):
        built.append(repo_config)
        return forge

    monkeypatch.setattr("robotsix_mill.scm.forge.base.get_forge", fake_get_forge)
    forge.built = built
    return forge


def _tools(**kw):
    read, grep = fft.build_fleet_file_tools(Settings(), **kw)
    return read, grep


def test_read_cited_workflow_resolves_provenance_flag(fleet, caplog):
    read, _ = _tools(ticket_id="T-7820", stage="review")
    with caplog.at_level(logging.INFO, logger=fft.__name__):
        out = read("robotsix-github-workflows", ".github/workflows/docker-release.yml")
    assert "--provenance mode=max" in out
    # ref defaults to the default branch (empty ref → forge default).
    assert fleet.reads == [(".github/workflows/docker-release.yml", "")]
    # Every call is logged with ticket id + repo + path.
    msg = caplog.text
    assert "T-7820" in msg
    assert "robotsix-github-workflows" in msg
    assert "docker-release.yml" in msg


def test_unregistered_repo_refused(fleet):
    read, grep = _tools()
    assert "not a registered fleet repo" in read("evil/repo", "README.md")
    assert "not a registered fleet repo" in grep("evil/repo", "x")
    assert fleet.built == [] and fleet.reads == []


def test_repo_without_remote_refused(fleet):
    read, _ = _tools()
    assert "no forge_remote_url" in read("no-remote", "README.md")


@pytest.mark.parametrize("path", ["../secrets", "a/../../b", "", "a\\b"])
def test_path_escape_rejected(fleet, path):
    read, _ = _tools()
    assert read("robotsix-github-workflows", path).startswith("error: invalid")
    assert fleet.reads == []


def test_missing_file_reports_error(fleet):
    read, _ = _tools()
    assert read("robotsix-github-workflows", "nope.txt").startswith("error:")


def test_read_capped_at_256kb(fleet):
    fleet.files["big.txt"] = "x" * (fft.READ_MAX_BYTES + 10_000)
    read, _ = _tools()
    out = read("robotsix-github-workflows", "big.txt")
    assert out.count("x") == fft.READ_MAX_BYTES
    assert "truncated" in out


def test_grep_finds_flag_with_glob(fleet):
    fleet.files["docs/other.md"] = "--provenance mentioned in docs"
    _, grep = _tools()
    out = grep("robotsix-github-workflows", r"--provenance", ".github/workflows/*.yml")
    assert out.startswith(".github/workflows/docker-release.yml:9:")
    assert "docs/other.md" not in out


def test_grep_capped_at_50_results(fleet):
    fleet.files["many.txt"] = "\n".join(["hit"] * 200)
    _, grep = _tools()
    out = grep("robotsix-github-workflows", "hit", "many.txt")
    assert out.count("many.txt:") == fft.GREP_MAX_RESULTS
    assert "stopped at 50 results" in out


def test_grep_invalid_regex(fleet):
    _, grep = _tools()
    assert "invalid regex" in grep("robotsix-github-workflows", "(")


def test_github_forge_reads_are_get_only():
    """The GitHub seam issues GETs only (read-only) with ref as a query param."""
    from robotsix_mill.scm.forge.github import GitHubForge

    calls: list = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _Http:
        def get(self, path, **kw):
            calls.append((path, kw))
            if "/git/trees/" in path:
                return _Resp({"tree": [{"type": "blob", "path": "a.yml"}]})
            return _Resp({"content": "aGk=", "encoding": "base64"})

    forge = object.__new__(GitHubForge)
    forge._http = _Http()
    assert forge._read_repo_file(owner="o", repo="r", path="x y.yml", ref="") == "hi"
    assert forge._read_repo_file(owner="o", repo="r", path="a.yml", ref="v1") == "hi"
    assert forge._list_repo_files(owner="o", repo="r", ref="") == ["a.yml"]
    assert calls[0] == ("/repos/o/r/contents/x%20y.yml", {"params": None})
    assert calls[1][1] == {"params": {"ref": "v1"}}
    assert calls[2][0] == "/repos/o/r/git/trees/HEAD"


def test_review_agent_verifies_cited_fleet_file_without_ask_user(
    fleet, tmp_path, monkeypatch
):
    """Review fixture citing robotsix-github-workflows' docker-release.yml:
    the reviewer resolves ``--provenance`` through ``read_fleet_file`` and
    approves instead of parking at NEEDS_DISCUSSION / [ASK_USER]."""
    import robotsix_mill.config as _cfg
    from robotsix_mill.agents.reviewing import ReviewVerdict, run_review_agent

    _reset_secrets()
    _cfg._secrets = Secrets(openrouter_api_key="k")
    captured: dict = {}

    class _Agent:
        def run_sync(self, prompt, **kwargs):
            tool = next(t for t in captured["tools"] if t.__name__ == "read_fleet_file")
            text = tool(
                "robotsix-github-workflows", ".github/workflows/docker-release.yml"
            )
            verified = "--provenance mode=max" in text
            return SimpleNamespace(
                output=ReviewVerdict(
                    verdict="APPROVE" if verified else "NEEDS_DISCUSSION",
                    comments="" if verified else "[ASK_USER] confirm flags",
                    auto_merge_eligible=verified,
                )
            )

    def fake_builder(*a, **k):
        captured.update(k)
        return _Agent()

    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent_from_definition", fake_builder
    )
    verdict = run_review_agent(
        settings=Settings(data_dir=str(tmp_path)),
        diff="diff --git a/docs/slsa.md b/docs/slsa.md\n+--provenance mode=max",
        spec=(
            "SLSA doc: robotsix-github-workflows/.github/workflows/"
            "docker-release.yml runs docker buildx build --provenance mode=max"
        ),
        ticket_id="T-7820",
    )
    assert verdict.verdict == "APPROVE"
    assert "[ASK_USER]" not in verdict.comments
    assert fleet.reads == [(".github/workflows/docker-release.yml", "")]
