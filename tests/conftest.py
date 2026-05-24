import pytest

from robotsix_mill.core import db
from robotsix_mill.config import Settings
from robotsix_mill.core.service import TicketService


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """Hard guarantee: the suite NEVER makes a real outbound HTTP
    request, so it can never consume OpenRouter / Langfuse / forge
    quota or tokens. Any test that forgot to mock the model/HTTP seam
    fails LOUDLY here instead of silently billing the account.

    Only real httpx transports are blocked. The FastAPI TestClient
    uses an in-process ASGI transport (not HTTPTransport), so API
    tests keep working untouched."""
    import httpx

    def _blocked(self, request, *a, **k):
        raise RuntimeError(
            f"Blocked real HTTP {request.method} {request.url} during "
            "tests. Tests must mock the model/HTTP seam — they must "
            "never hit OpenRouter/Langfuse/the forge or consume tokens."
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", _blocked
    )


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Hermeticity: never let the developer's ./.env or ./secrets.env leak into
    tests (they can carry a real OPENROUTER_API_KEY / FORGE_REMOTE_URL and make
    the suite hit the network). Disable env_file for every Settings()
    AND clear EVERY ambient credential/endpoint var — pydantic-settings
    reads os.environ regardless of env_file, so anything exported in the
    shell *or in the running mill container* (where the implement stage
    runs the suite as its gate) leaks in. An unstripped LANGFUSE_*/
    FORGE_*/NTFY_* flips tracing_enabled / forge-config on and makes
    hermetic tests assert wrong or hit the network — which made the
    full-suite implement gate fail in-container (76 env-driven
    failures) and BLOCK essentially every ticket. The suite must be
    identical green on a clean machine and inside the container.

    Also blocks YAML overlay files (``config/mill.local.yaml``,
    ``config/mill.production.yaml``, ``config/secrets.yaml``) by
    setting ``MILL_CONFIG_FILE`` and ``MILL_SECRETS_FILE`` to empty
    so only the committed ``config/mill.defaults.yaml`` is loaded."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    # Block YAML overlays — only defaults.yaml is loaded in tests.
    monkeypatch.setenv("MILL_CONFIG_FILE", "")
    monkeypatch.setenv("MILL_SECRETS_FILE", "")
    # Patch _LOCAL_FILE to a nonexistent path so no local overlay
    # leaks into any test, even when Settings.__init__ calls
    # load_yaml_config directly.
    import robotsix_mill.config_loader as _cl
    monkeypatch.setattr(_cl, "_LOCAL_FILE", _cl.Path("/nonexistent/mill.local.yaml"))
    for var in (
        "OPENROUTER_API_KEY",
        "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL",
        "FORGE_KIND", "FORGE_REMOTE_URL", "FORGE_TOKEN", "FORGE_AUTH",
        "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "NTFY_URL", "NTFY_TOKEN",
        # Deployment/DinD-only knob: the container sets this so the
        # sandbox bind-mounts the host ./data. Leaking it flips
        # _repo_mount from the named-volume branch to the bind branch,
        # breaking test_sandbox argv assertions in-container.
        "MILL_SANDBOX_DATA_MOUNT",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    db.reset_engine()  # don't reuse a cached engine across tests
    # Default to autonomous mode so existing tests that depend on
    # refine→ready chaining stay green without per-test overrides.
    s = Settings(MILL_DATA_DIR=str(tmp_path), MILL_REQUIRE_APPROVAL="false")
    db.init_db(s)
    yield s
    db.reset_engine()


@pytest.fixture
def service(settings) -> TicketService:
    return TicketService(settings)


@pytest.fixture
def fake_sandbox(monkeypatch):
    """Replace the (always-containerized) sandbox seam with a tiny
    interpreter so the suite is hermetic and never invokes Docker.
    There is no 'local' mode to fall back on by design."""
    from robotsix_mill import sandbox

    def _run(command, *, repo_dir, settings, epic_workspace_path=None):
        c = command.strip()
        if c == "false":
            return (1, "false: command failed")
        if c.startswith("echo "):
            return (0, c[5:] + "\n")
        return (0, "")  # "true", "", and anything else: success

    def _fetch(url, *, settings):
        return (0, f"<fake page for {url}>")

    monkeypatch.setattr(sandbox, "run", _run)
    monkeypatch.setattr(sandbox, "fetch", _fetch)
    return _run
