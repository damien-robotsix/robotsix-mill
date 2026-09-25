"""Tests for scripts/resolve_docker_digest.py — all network calls are mocked."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.script_loader import load_script

resolve_docker_digest = load_script(Path("scripts/resolve_docker_digest.py"))


class TestHubApiDigest:
    def test_resolves_amd64_digest(self):
        """Picks the linux/amd64 digest when multiple architectures exist."""
        fake_response = {
            "images": [
                {
                    "architecture": "amd64",
                    "os": "linux",
                    "digest": "sha256:aaa111",
                },
                {
                    "architecture": "arm64",
                    "os": "linux",
                    "digest": "sha256:bbb222",
                },
            ]
        }
        mock = MagicMock()
        mock.read.return_value = json.dumps(fake_response).encode()
        mock.__enter__.return_value = mock

        with patch.object(urllib.request, "urlopen", return_value=mock):
            digest = resolve_docker_digest._hub_api_digest(
                "python", "3.14-slim", "linux/amd64"
            )
        assert digest == "sha256:aaa111"

    def test_fallback_first_digest_when_no_platform_match(self):
        """Returns first available digest when target platform is absent."""
        fake_response = {
            "images": [
                {
                    "architecture": "arm64",
                    "os": "linux",
                    "digest": "sha256:ccc333",
                },
            ]
        }
        mock = MagicMock()
        mock.read.return_value = json.dumps(fake_response).encode()
        mock.__enter__.return_value = mock

        with patch.object(urllib.request, "urlopen", return_value=mock):
            digest = resolve_docker_digest._hub_api_digest(
                "alpine", "3.20", "linux/amd64"
            )
        assert digest == "sha256:ccc333"

    def test_exits_when_no_images(self):
        """SystemExit when the response has no images array."""
        fake_response = {"images": []}
        mock = MagicMock()
        mock.read.return_value = json.dumps(fake_response).encode()
        mock.__enter__.return_value = mock

        with patch.object(urllib.request, "urlopen", return_value=mock):
            with pytest.raises(SystemExit):
                resolve_docker_digest._hub_api_digest(
                    "python", "nonexistent", "linux/amd64"
                )


class TestResolveDigest:
    def test_bare_image_uses_hub_api(self):
        """Bare image name ('python') hits the Hub API."""
        fake_response = {
            "images": [
                {"architecture": "amd64", "os": "linux", "digest": "sha256:ddd444"}
            ]
        }
        mock = MagicMock()
        mock.read.return_value = json.dumps(fake_response).encode()
        mock.__enter__.return_value = mock

        with patch.object(urllib.request, "urlopen", return_value=mock):
            digest = resolve_docker_digest.resolve_digest("python:3.14-slim")
        assert digest == "sha256:ddd444"

    def test_library_prefix_uses_hub_api(self):
        """'library/python' still uses the Hub API."""
        fake_response = {
            "images": [
                {"architecture": "amd64", "os": "linux", "digest": "sha256:eee555"}
            ]
        }
        mock = MagicMock()
        mock.read.return_value = json.dumps(fake_response).encode()
        mock.__enter__.return_value = mock

        with patch.object(urllib.request, "urlopen", return_value=mock):
            digest = resolve_docker_digest.resolve_digest("library/python:3.14-slim")
        assert digest == "sha256:eee555"

    def test_dockerhub_user_repo_uses_registry_api(self):
        """'namespace/image' on Docker Hub uses the registry v2 token+manifest flow."""
        token_mock = MagicMock()
        token_mock.read.return_value = json.dumps({"token": "test-token"}).encode()
        token_mock.__enter__.return_value = token_mock

        manifest_mock = MagicMock()
        manifest_mock.headers = {"Docker-Content-Digest": "sha256:fff666"}
        manifest_mock.__enter__.return_value = manifest_mock

        call_count = 0
        responses = [token_mock, manifest_mock]

        def side_effect(req, **kwargs):
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        with patch.object(urllib.request, "urlopen", side_effect=side_effect):
            digest = resolve_docker_digest.resolve_digest("myorg/myimage:v1")
        assert digest == "sha256:fff666"

    def test_dockerhub_non_library_manifest_url_uses_registry_host(self):
        """The manifest URL uses registry-1.docker.io, not auth.docker.io."""
        token_mock = MagicMock()
        token_mock.read.return_value = json.dumps({"token": "test-token"}).encode()
        token_mock.__enter__.return_value = token_mock

        manifest_mock = MagicMock()
        manifest_mock.headers = {"Docker-Content-Digest": "sha256:registry-digest"}
        manifest_mock.__enter__.return_value = manifest_mock

        captured_urls = []
        call_count = 0
        responses = [token_mock, manifest_mock]

        def side_effect(req, **kwargs):
            nonlocal call_count
            captured_urls.append(req.get_full_url())
            resp = responses[call_count]
            call_count += 1
            return resp

        with patch.object(urllib.request, "urlopen", side_effect=side_effect):
            digest = resolve_docker_digest.resolve_digest("myorg/myimage:v1")

        assert digest == "sha256:registry-digest"
        assert any("auth.docker.io/token" in u for u in captured_urls), (
            f"token URL should use auth.docker.io, got: {captured_urls}"
        )
        assert any("registry-1.docker.io/v2/" in u for u in captured_urls), (
            f"manifest URL should use registry-1.docker.io, got: {captured_urls}"
        )
        assert not any("auth.docker.io/v2/" in u for u in captured_urls), (
            f"manifest URL should NOT use auth.docker.io, got: {captured_urls}"
        )

    def test_ghcr_image(self):
        """ghcr.io images use the registry v2 token+manifest flow."""
        token_mock = MagicMock()
        token_mock.read.return_value = json.dumps({"token": "test-token"}).encode()
        token_mock.__enter__.return_value = token_mock

        manifest_mock = MagicMock()
        manifest_mock.headers = {"Docker-Content-Digest": "sha256:ggg777"}
        manifest_mock.__enter__.return_value = manifest_mock

        call_count = 0
        responses = [token_mock, manifest_mock]

        def side_effect(req, **kwargs):
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        with patch.object(urllib.request, "urlopen", side_effect=side_effect):
            digest = resolve_docker_digest.resolve_digest("ghcr.io/owner/image:v2")
        assert digest == "sha256:ggg777"

    def test_missing_tag_exits(self):
        """Missing tag raises SystemExit."""
        with pytest.raises(SystemExit):
            resolve_docker_digest.resolve_digest("no-tag-here")


class TestMain:
    def test_main_prints_digest(self, capsys):
        """main() prints the digest to stdout."""
        fake_response = {
            "images": [
                {"architecture": "amd64", "os": "linux", "digest": "sha256:hhh888"}
            ]
        }
        mock = MagicMock()
        mock.read.return_value = json.dumps(fake_response).encode()
        mock.__enter__.return_value = mock

        with patch.object(urllib.request, "urlopen", return_value=mock):
            with patch.object(
                resolve_docker_digest.sys,
                "argv",
                ["resolve_docker_digest.py", "python:3.14-slim"],
            ):
                resolve_docker_digest.main()

        captured = capsys.readouterr()
        assert "sha256:hhh888" in captured.out

    def test_main_platform_flag(self, capsys):
        """main() respects --platform flag."""
        fake_response = {
            "images": [
                {"architecture": "arm64", "os": "linux", "digest": "sha256:arm999"}
            ]
        }
        mock = MagicMock()
        mock.read.return_value = json.dumps(fake_response).encode()
        mock.__enter__.return_value = mock

        with patch.object(urllib.request, "urlopen", return_value=mock):
            with patch.object(
                resolve_docker_digest.sys,
                "argv",
                [
                    "resolve_docker_digest.py",
                    "python:3.14-slim",
                    "--platform",
                    "linux/arm64",
                ],
            ):
                resolve_docker_digest.main()

        captured = capsys.readouterr()
        assert "sha256:arm999" in captured.out

    def test_main_no_args_exits(self):
        """main() with no arguments exits."""
        with patch.object(
            resolve_docker_digest.sys, "argv", ["resolve_docker_digest.py"]
        ):
            with pytest.raises(SystemExit):
                resolve_docker_digest.main()
