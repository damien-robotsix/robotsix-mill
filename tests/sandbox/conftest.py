import pytest


@pytest.fixture(autouse=True)
def _neutralize_docker_host(monkeypatch):
    """Keep the sandbox tests hermetic w.r.t. the ambient ``DOCKER_HOST``.

    ``sandbox.run()`` only performs deploy-mode egress-network setup
    (``ensure_sandbox_network``) when ``DOCKER_HOST`` is set. When the suite
    runs inside a mill sandbox container, ``DOCKER_HOST`` points at the
    socket-proxy, so tests that mock ``subprocess.run`` (returning *bytes*
    stderr, matching ``run()``'s decode contract) would drive the real
    ``ensure_sandbox_network`` — which uses ``text=True`` and does
    ``"already exists" not in stderr``, raising ``TypeError`` on bytes.

    Default the whole module to dev mode (no ``DOCKER_HOST``); the two tests
    that exercise deploy-mode network setup set it explicitly in their body.
    """
    monkeypatch.delenv("DOCKER_HOST", raising=False)
