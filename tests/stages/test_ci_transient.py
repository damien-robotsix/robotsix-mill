"""Tests for the transient CI failure classifier (ci_transient.py)."""

from __future__ import annotations

from robotsix_mill.stages.ci_transient import is_transient_ci_failure


class TestIsTransientCiFailure:
    """Unit tests for the transient-failure pattern matcher."""

    def test_returns_false_for_empty_summary(self):
        assert is_transient_ci_failure("") is False

    def test_returns_false_for_deterministic_lint_failure(self):
        summary = (
            "## ❌ FAILED: ruff\n\n"
            "**Annotations:**\n"
            "- [failure] src/mod.py:42: F841 local variable `x` is assigned but never used\n"
        )
        assert is_transient_ci_failure(summary) is False

    def test_returns_false_for_deterministic_test_failure(self):
        summary = (
            "## ❌ FAILED: pytest\n\n"
            "**Summary:** 1 test failed\n"
            "**Details:**\n"
            "FAILED tests/test_foo.py::test_bar - AssertionError: assert 1 == 2\n"
        )
        assert is_transient_ci_failure(summary) is False

    def test_returns_false_for_deterministic_type_check_failure(self):
        summary = (
            "## ❌ FAILED: mypy\n\n"
            "**Annotations:**\n"
            "- [failure] src/mod.py:10: Incompatible types in assignment\n"
        )
        assert is_transient_ci_failure(summary) is False

    # --- ECONNRESET / network reset ---

    def test_detects_econnreset(self):
        summary = (
            "## ❌ FAILED: CodeQL\n\n"
            "**Job logs:**\n"
            "```\n"
            "Run github/codeql-action/analyze@v3\n"
            "Error: ECONNRESET\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_connection_reset_by_peer(self):
        summary = (
            "## ❌ FAILED: pre-commit\n\n"
            "**Job logs:**\n"
            "```\n"
            "curl: (56) Recv failure: Connection reset by peer\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_connection_refused(self):
        summary = (
            "## ❌ FAILED: npm-install\n\n"
            "**Job logs:**\n"
            "```\n"
            "npm ERR! network request to https://registry.npmjs.org/ failed\n"
            "npm ERR! network This is a problem related to network connectivity.\n"
            "npm ERR! network connect ECONNREFUSED\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    # --- buildx / Docker ---

    def test_detects_booting_buildkit(self):
        summary = (
            "## ❌ FAILED: release-image\n\n"
            "**Job logs:**\n"
            "```\n"
            "ERROR: Cannot connect to the Docker daemon\n"
            "booting buildkit: failed to start\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_cannot_boot_buildkit(self):
        summary = (
            "## ❌ FAILED: build-and-push\n\n"
            "**Job logs:**\n"
            "```\n"
            "cannot boot buildkit: container unexpectedly exited\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    # --- setup-uv / action fetcher ---

    def test_detects_setup_uv_failure(self):
        summary = (
            "## ❌ FAILED: setup-uv\n\n"
            "**Job logs:**\n"
            "```\n"
            "Run astral-sh/setup-uv@v3\n"
            "Error: setup-uv action failed: unable to download uv binary\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_failed_to_download_action(self):
        summary = (
            "## ❌ FAILED: checkout\n\n"
            "**Job logs:**\n"
            "```\n"
            "Failed to download action 'actions/checkout@v4'.\n"
            "Error: unable to get the latest version\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    # --- runner infrastructure ---

    def test_detects_runner_shutdown_signal(self):
        summary = (
            "## ❌ FAILED: Build\n\n"
            "**Job logs:**\n"
            "```\n"
            "The runner has received a shutdown signal. This job will be\n"
            "requeued or abandoned.\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_runner_lost_communication(self):
        summary = (
            "## ❌ FAILED: deploy\n\n"
            "**Job logs:**\n"
            "```\n"
            "The self-hosted runner: runner-xyz lost communication with the server.\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    # --- API rate limiting / 5xx ---

    def test_detects_http_502(self):
        summary = (
            "## ❌ FAILED: release-image\n\n"
            "**Job logs:**\n"
            "```\n"
            "HTTP 502 Bad Gateway\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_http_503(self):
        summary = (
            "## ❌ FAILED: deploy-check\n\n"
            "**Job logs:**\n"
            "```\n"
            "HTTP 503 Service Unavailable\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_api_rate_limit_exceeded(self):
        summary = (
            "## ❌ FAILED: github-api\n\n"
            "**Job logs:**\n"
            "```\n"
            "API rate limit exceeded for installation ID 123456\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_secondary_rate_limit(self):
        summary = (
            "## ❌ FAILED: github-api\n\n"
            "**Job logs:**\n"
            "```\n"
            "You have exceeded a secondary rate limit.\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    # --- package manager fetch failures ---

    def test_detects_npm_network_unreachable(self):
        summary = (
            "## ❌ FAILED: frontend-build\n\n"
            "**Job logs:**\n"
            "```\n"
            "npm ERR! network request to https://registry.npmjs.org/ failed, reason:\n"
            "network unreachable\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_could_not_resolve_host(self):
        summary = (
            "## ❌ FAILED: apt-install\n\n"
            "**Job logs:**\n"
            "```\n"
            "Could not resolve host: archive.ubuntu.com\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_temporary_failure_resolving(self):
        summary = (
            "## ❌ FAILED: pip-install\n\n"
            "**Job logs:**\n"
            "```\n"
            "Temporary failure resolving 'pypi.org'\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_multiple_transient_signals_still_transient(self):
        """A summary with multiple transient signals should still return True."""
        summary = (
            "## ❌ FAILED: combined\n\n"
            "**Job logs:**\n"
            "```\n"
            "ECONNRESET\n"
            "HTTP 502 Bad Gateway\n"
            "The runner has received a shutdown signal.\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_failing_param_is_accepted_but_ignored(self):
        """The `failing` list param is accepted but currently ignored."""
        assert is_transient_ci_failure("", failing=[{"name": "CodeQL"}]) is False
        assert (
            is_transient_ci_failure("ECONNRESET in logs", failing=[{"name": "test"}])
            is True
        )

    def test_pattern_match_in_annotation_not_just_logs(self):
        """Transient patterns are matched anywhere in the summary, including
        check annotations, not just the job logs section."""
        summary = (
            "## ❌ FAILED: HTTP Check\n\n"
            "**Annotations:**\n"
            "- [failure] : HTTP 503 Service Unavailable from api.github.com\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_deterministic_failure_with_http_in_name_not_misclassified(self):
        """A deterministic failure whose check name happens to contain 'HTTP'
        should not be misclassified as transient (only full pattern matches)."""
        summary = (
            "## ❌ FAILED: HTTP Integration Test\n\n"
            "**Details:**\n"
            "FAILED tests/test_http.py::test_post - assert 200 == 400\n"
        )
        assert is_transient_ci_failure(summary) is False

    # --- runner / action-setup fetch failures ---

    def test_detects_setup_uv_fetch_failed_marker(self):
        """AC #1: a setup-uv step that dies with ``##[error]fetch failed`` on
        a log line separate from the ``Run astral-sh/setup-uv`` header is
        still classified infra-transient."""
        summary = (
            "## ❌ FAILED: Baseline (shared) / Modules drift\n\n"
            "**Job logs:**\n"
            "```\n"
            "Run astral-sh/setup-uv@v6\n"
            "Downloading uv from ...\n"
            "##[error]fetch failed\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_setup_python_fetch_failed(self):
        summary = (
            "## ❌ FAILED: Tests\n\n"
            "**Job logs:**\n"
            "```\n"
            "actions/setup-python fetch failed while downloading manifest\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_checkout_failed_to_download(self):
        summary = (
            "## ❌ FAILED: Build\n\n"
            "**Job logs:**\n"
            "```\n"
            "actions/checkout failed to download the repository archive\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_unable_to_resolve_action(self):
        summary = (
            "## ❌ FAILED: setup\n\n"
            "**Job logs:**\n"
            "```\n"
            "##[error]Unable to resolve action `astral-sh/setup-uv@v6`\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_hosted_runner_lost_communication(self):
        summary = (
            "## ❌ FAILED: Build\n\n"
            "**Job logs:**\n"
            "```\n"
            "The hosted runner: GitHub Actions 5 lost communication with "
            "the server.\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_httperror_rate_limit(self):
        summary = (
            "## ❌ FAILED: publish\n\n"
            "**Job logs:**\n"
            "```\n"
            "HttpError: rate limit hit while querying the API\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_docker_hub_too_many_requests(self):
        summary = (
            "## ❌ FAILED: release-image\n\n"
            "**Job logs:**\n"
            "```\n"
            "toomanyrequests: You have reached your pull rate limit.\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_docker_registry_5xx_pull(self):
        summary = (
            "## ❌ FAILED: release-image\n\n"
            "**Job logs:**\n"
            "```\n"
            "failed to pull image: received unexpected HTTP status: 503 "
            "Service Unavailable\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_pypi_mirror_5xx(self):
        summary = (
            "## ❌ FAILED: install\n\n"
            "**Job logs:**\n"
            "```\n"
            "uv failed to fetch from pypi: 502 Bad Gateway\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_apt_mirror_5xx(self):
        summary = (
            "## ❌ FAILED: setup\n\n"
            "**Job logs:**\n"
            "```\n"
            "apt-get update failed: 503 Service Unavailable\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_plain_fetch_failed_without_marker_not_misclassified(self):
        """A test that prints 'fetch failed' without the runner ``##[error]``
        marker or an action name must not be classified transient."""
        summary = (
            "## ❌ FAILED: unit tests\n\n"
            "**Details:**\n"
            "FAILED tests/test_client.py::test_fetch - AssertionError: "
            "fetch failed for stub response\n"
        )
        assert is_transient_ci_failure(summary) is False

    def test_detects_htmlproofer_external_link_5xx(self):
        """mkdocs htmlproofer hitting a 504 on a link outside the diff."""
        summary = (
            "## ❌ FAILED: docs\n\n"
            "**Job logs:**\n"
            "```\n"
            "- ./index.html\n"
            "  *  External link https://slsa.dev/spec/v1.2/ failed\n"
            "     response code 504 means something's wrong\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_bare_504_gateway_timeout(self):
        summary = (
            "## ❌ FAILED: link-check\n\n"
            "**Job logs:**\n"
            "```\n"
            "https://slsa.dev/spec/v1.2/ -> 504 Gateway Timeout\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_npm_audit_registry_503(self):
        """`npm audit` hitting a 503 from registry.npmjs.org (JS lint job)."""
        summary = (
            "## ❌ FAILED: js-lint\n\n"
            "**Job logs:**\n"
            "```\n"
            "npm error code E503\n"
            "npm error 503 Service Unavailable - "
            "GET https://registry.npmjs.org/-/npm/v1/security/audits\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True

    def test_detects_registry_npmjs_org_5xx(self):
        summary = (
            "## ❌ FAILED: install\n\n"
            "**Job logs:**\n"
            "```\n"
            "request to https://registry.npmjs.org/left-pad failed, 500\n"
            "```\n"
        )
        assert is_transient_ci_failure(summary) is True
