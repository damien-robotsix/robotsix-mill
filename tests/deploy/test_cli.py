"""Tests for the deploy-server CLI entrypoint."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robotsix_deploy.cli import main


def test_main_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    """main exits non-zero for an unknown subcommand."""
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert "invalid choice" in captured.err


def test_main_serve_starts() -> None:
    """main("serve") parses args and starts uvicorn."""
    with patch("robotsix_deploy.cli.uvicorn.run") as mock_run:
        main(["serve"])
    mock_run.assert_called_once()


def test_main_serve_custom_args() -> None:
    """main("serve") respects --host, --port, --log-level overrides."""
    with patch("robotsix_deploy.cli.uvicorn.run") as mock_run:
        main(["serve", "--host", "0.0.0.0", "--port", "9090", "--log-level", "debug"])
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9090
    assert kwargs["log_level"] == "debug"


def test_main_serve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """main("serve") falls back to DeploySettings env defaults."""
    monkeypatch.setenv("DEPLOY_HOST", "10.0.0.1")
    monkeypatch.setenv("DEPLOY_PORT", "3000")
    with patch("robotsix_deploy.cli.uvicorn.run") as mock_run:
        main(["serve"])
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs["host"] == "10.0.0.1"
    assert kwargs["port"] == 3000


def test_main_serve_print_help(capsys: pytest.CaptureFixture[str]) -> None:
    """main with no args prints help and exits."""
    # When argv is empty, argparse parses sys.argv which is ["pytest"].
    # Force the "no command" path by passing an explicit empty list.
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out
