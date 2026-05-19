import pytest

from robotsix_mill.core import db
from robotsix_mill.config import Settings
from robotsix_mill.core.service import TicketService


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Hermeticity: never let the developer's ./.env leak into tests
    (it can carry a real OPENROUTER_API_KEY / FORGE_REMOTE_URL and make
    the suite hit the network). Disable env_file for every Settings()
    AND clear EVERY ambient credential/endpoint var — pydantic-settings
    reads os.environ regardless of env_file, so anything exported in the
    shell *or in the running mill container* (where the implement stage
    runs the suite as its gate) leaks in. An unstripped LANGFUSE_*/
    FORGE_*/NTFY_* flips tracing_enabled / forge-config on and makes
    hermetic tests assert wrong or hit the network — which made the
    full-suite implement gate fail in-container (76 env-driven
    failures) and BLOCK essentially every ticket. The suite must be
    identical green on a clean machine and inside the container."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for var in (
        "OPENROUTER_API_KEY",
        "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL",
        "FORGE_KIND", "FORGE_REMOTE_URL", "FORGE_TOKEN", "FORGE_AUTH",
        "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "NTFY_URL", "NTFY_TOKEN",
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

    def _run(command, *, repo_dir, settings):
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
