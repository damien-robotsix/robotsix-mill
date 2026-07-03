import pytest

from robotsix_mill.core import db
from robotsix_mill.config import RepoConfig, ReposRegistry, Settings
from robotsix_mill.core.service import TicketService


@pytest.fixture(autouse=True, scope="session")
def _isolate_default_data_dir(tmp_path_factory):
    """Redirect bare ``Settings()`` constructions to a session tmp dir.

    Tests that pass ``data_dir=...`` explicitly keep their override
    (kwargs win at pydantic-settings' init layer). Tests that build a
    bare ``Settings()`` — directly or via a runner that calls it
    internally — get the session sandbox instead of the project's
    real ``.data/`` directory.

    Mechanics: monkey-patch ``JsonSettingsSource.__call__`` so its
    returned dict carries ``data_dir = <session sandbox>``. Anything
    higher-priority (kwargs, env vars) still overrides.
    ``load_config()`` itself is NOT patched, so tests that
    inspect raw JSON defaults continue to see ``.data``.
    """
    sandbox = tmp_path_factory.mktemp("mill-default-data")
    from robotsix_mill import config as _cfg

    real_call = _cfg.JsonSettingsSource.__call__

    def patched(self):
        result = real_call(self)
        result["data_dir"] = str(sandbox)
        return result

    _cfg.JsonSettingsSource.__call__ = patched
    try:
        yield sandbox
    finally:
        _cfg.JsonSettingsSource.__call__ = real_call


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
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Hermeticity: never let the developer's ambient env vars leak into
    tests (they can carry a real OPENROUTER_API_KEY / FORGE_REMOTE_URL and make
    the suite hit the network). Settings no longer reads .env/secrets.env
    files; it only reads ``os.environ`` and ``config/*.yaml``.  Clear
    EVERY ambient credential/endpoint var — pydantic-settings reads
    os.environ, so anything exported in the shell *or in the running mill
    container* (where the implement stage runs the suite as its gate)
    leaks in. An unstripped LANGFUSE_*/FORGE_*/NTFY_* flips
    tracing_enabled / forge-config on and makes hermetic tests assert
    wrong or hit the network — which made the full-suite implement gate
    fail in-container (76 env-driven failures) and BLOCK essentially
    every ticket. The suite must be identical green on a clean machine
    and inside the container.

    Pins the config loader to the committed ``config/config.example.yaml``
    template (never the gitignored, host-specific ``config/config.yaml``)
    by setting ``MILL_CONFIG_FILE`` and ``MILL_SECRETS_FILE`` to empty —
    the loader treats ``""`` as "use the committed example", whose
    secrets are all the ``SECRET`` sentinel (i.e. unset)."""
    monkeypatch.setenv("MILL_CONFIG_FILE", "")
    monkeypatch.setenv("MILL_SECRETS_FILE", "")
    monkeypatch.setenv("MILL_REPOS_FILE", "")
    # Disable the board-poll list cache by default in tests so GET /tickets
    # is always immediately consistent (create-then-list). The committed
    # config.example.yaml enables it (3.0s) in real deployments; env beats
    # YAML, so this override is inert in production. Tests that exercise the
    # cache opt in explicitly via Settings(board_list_cache_ttl_seconds=…)
    # (a kwarg, which beats env). Also clear the module-global cache so no
    # entry leaks across tests.
    monkeypatch.setenv("MILL_BOARD_LIST_CACHE_TTL_SECONDS", "0")
    from robotsix_mill.runtime.routes import _tickets as _tickets_routes

    _tickets_routes._LIST_CACHE.clear()
    for var in (
        "OPENROUTER_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "FORGE_KIND",
        "FORGE_REMOTE_URL",
        "FORGE_TOKEN",
        "FORGE_AUTH",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "NTFY_URL",
        "NTFY_TOKEN",
        # Deployment/DinD-only knob: the container sets this so the
        # sandbox bind-mounts the host ./data. Leaking it flips
        # _repo_mount from the named-volume branch to the bind branch,
        # breaking test_sandbox argv assertions in-container.
        "MILL_SANDBOX_DATA_MOUNT",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _reset_secrets_each_test():
    """Clear the cached Secrets singleton before every test so no
    test leaks secret values into another."""
    from robotsix_mill.config import _reset_repos_config, _reset_secrets

    _reset_secrets()
    _reset_repos_config()


@pytest.fixture(autouse=True)
def _restore_tool_registry():
    """Keep the module-global ``ToolRegistry`` order-independent.

    The registry leaks in *both* directions across tests, and serial
    runs mask it via execution order. Some tests call
    ``ToolRegistry._tools.clear()`` to exercise tool self-registration
    in isolation; others register lazily-built tools (e.g.
    ``parallel_explore``, registered only when its tool-maker is called
    while building an explore-enabled agent) that then linger in the
    global catalog. Under ``pytest-xdist`` (CI runs
    ``-n auto --dist loadscope``) either kind of mutation can land on
    the same worker just before a test whose build-time tool-directive
    guard reads ``ToolRegistry.list_tools()``: a cleared catalog makes
    it under-report tools, while a leaked-in tool makes it flag a prompt
    directive for a tool that test's agent legitimately lacks (e.g. the
    audit agent built without ``repo_dir`` lacks ``parallel_explore``).

    Snapshot the registry before each test and restore it *exactly*
    afterwards — re-adding entries a test removed and dropping entries a
    test added — so every test sees the same import-time catalog
    regardless of execution order."""
    from robotsix_mill.agents.tool_registry import ToolRegistry

    snapshot = dict(ToolRegistry._tools)
    try:
        yield
    finally:
        ToolRegistry._tools.clear()
        ToolRegistry._tools.update(snapshot)


@pytest.fixture
def secrets_set():
    """Fixture that lets tests inject secret values into ``get_secrets()``.

    Returns a callable ``set(**overrides)`` that constructs a fresh
    ``Secrets`` with the given overrides and stores it directly on
    the config module's ``_secrets`` singleton so that every module
    that calls ``get_secrets()`` (even those that imported it at
    module level) sees the test values.
    """
    from robotsix_mill.config import Secrets, _reset_secrets
    import robotsix_mill.config as _cfg

    def _set(**overrides):
        _reset_secrets()
        _cfg._secrets = Secrets(**overrides)

    return _set


@pytest.fixture
def settings(tmp_path) -> Settings:
    db.reset_engine()  # don't reuse a cached engine across tests
    # Default to autonomous mode so existing tests that depend on
    # refine→ready chaining stay green without per-test overrides.
    s = Settings(data_dir=str(tmp_path), require_approval="false")
    # Single-repo deployments now live under <data_dir>/<board_id>/.
    # Match the default ``service`` fixture below.
    db.init_db(s, board_id="test-board")
    yield s
    db.reset_engine()


@pytest.fixture
def service(settings) -> TicketService:
    return TicketService(settings, board_id="test-board")


@pytest.fixture
def repo_config() -> RepoConfig:
    return RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test-project",
        langfuse_project_id="",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


@pytest.fixture
def repos_registry(repo_config: RepoConfig) -> ReposRegistry:
    return ReposRegistry(repos={repo_config.repo_id: repo_config})


@pytest.fixture
def two_repo_registry() -> ReposRegistry:
    """Two distinct repos for multi-repo isolation tests.

    Uses the same repo_id/board_id/project mapping that the ticket
    spec mandates so assertions are unambiguous.
    """
    return ReposRegistry(
        repos={
            "repo-a": RepoConfig(
                repo_id="repo-a",
                board_id="board-a",
                langfuse_project_name="proj-a",
                langfuse_public_key="pk-a",
                langfuse_secret_key="sk-a",
            ),
            "repo-b": RepoConfig(
                repo_id="repo-b",
                board_id="board-b",
                langfuse_project_name="proj-b",
                langfuse_public_key="pk-b",
                langfuse_secret_key="sk-b",
            ),
        }
    )


@pytest.fixture
def fake_sandbox(monkeypatch):
    """Replace the (always-containerized) sandbox seam with a tiny
    interpreter so the suite is hermetic and never invokes Docker.
    There is no 'local' mode to fall back on by design."""
    from robotsix_mill import sandbox

    def _run(command, *, repo_dir, settings, **kwargs):
        # Accept any extra keyword (e.g. install_project from the test
        # gate) so the fake tolerates sandbox.run signature growth.
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
