"""The test sub-agent.

Runs the project's test command in the isolated sandbox (mechanical,
deterministic), then — on failure — a CHEAP model distills the raw
output into a short, actionable diagnosis the coordinator can turn
into the next precise implement instruction. The coordinator never
sees the full log; its history stays short.

``run_test_agent`` is the mockable seam.
"""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib
from pathlib import Path, PurePath

from ..config import RepoConfig, Settings, get_secrets
from ..config.repo_settings import load_repo_smoke_command, load_repo_test_command

# Machine-detectable marker prefixing a deterministic, LLM-free diagnosis
# for an ENVIRONMENTAL test-gate failure (a missing binary the agent cannot
# fix by editing code). ``stages/implement.py`` imports this to drive its
# fix-loop circuit breaker, keyed off the diagnosis being byte-identical
# across cycles for the same missing binary.
ENV_ERROR_PREFIX = "ENV-ERROR:"

# Command-not-found signatures from the two common shells:
#   dash / ``sh -lc``:  "sh: 1: yamllint: not found"
#   bash:               "yamllint: command not found"
_SH_NOT_FOUND_RE = re.compile(r"sh: \d+: ([^:\n]+): not found")
_BASH_NOT_FOUND_RE = re.compile(r"(?:^|\n)\s*([\w./+-]+): command not found")

# Permission-denied signature (rc 126): an existing file that resolved but
# could not EXECUTE. We only treat it as environmental when the path points
# into the sandbox HOME ($HOME/.local/bin = /tmp/.local/bin) or /tmp — the
# fingerprint of a pip --user console script blocked by a noexec tmpfs, not
# a buggy repo script.
_PERM_DENIED_RE = re.compile(r"((?:/tmp|\S*\.local/bin)\S*): Permission denied")


def _detect_missing_binary(out: str) -> str | None:
    """Extract the missing binary name from a command-not-found message.

    Returns the binary name, or ``None`` if no command-not-found
    signature is present.
    """
    m = _SH_NOT_FOUND_RE.search(out)
    if m:
        return m.group(1).strip()
    m = _BASH_NOT_FOUND_RE.search(out)
    if m:
        return m.group(1).strip()
    return None


def _detect_noexec_script(out: str) -> str | None:
    """Extract a Permission-denied path under the sandbox HOME
    (``/tmp/.local/bin``) or ``/tmp``.

    Returns the offending path, or ``None`` when no such signature is
    present — the fingerprint of a pip --user console script blocked by a
    ``noexec`` tmpfs.
    """
    m = _PERM_DENIED_RE.search(out)
    if m:
        return m.group(1).strip()
    return None


def _env_error_diag(rc: int, out: str) -> str | None:
    """Return a STABLE, LLM-free diagnosis for an environmental failure —
    a binary referenced by the gate command is not installed / not on
    PATH — or ``None`` when the failure is not environmental.

    Triggers on ``rc == 127`` OR an explicit command-not-found signature,
    OR ``rc == 126`` with a Permission-denied signature on a
    ``$HOME/.local/bin`` (or ``/tmp``) path — a pip --user console script
    blocked by a ``noexec`` tmpfs (conservative: a normal assertion
    failure must NOT match). The string is byte-identical across cycles
    for the same failure, which is what lets the implement fix-loop
    circuit breaker fire.
    """
    missing_bin = _detect_missing_binary(out)
    if rc != 127 and missing_bin is None:
        # Not command-not-found, but rc 126 + a Permission-denied signature
        # on a $HOME/.local/bin (or /tmp) path means a pip --user console
        # script could not EXECUTE because the sandbox /tmp tmpfs is mounted
        # noexec. Classify it so the fix-loop circuit breaker fires if the
        # exec tmpfs mount ever regresses.
        noexec_path = _detect_noexec_script(out)
        if rc == 126 and noexec_path is not None:
            return (
                f"{ENV_ERROR_PREFIX} command not executable in sandbox: "
                f"'{noexec_path}' (rc={rc}). A pip --user console script "
                "under $HOME/.local/bin could not execute — the sandbox "
                "/tmp tmpfs must be mounted exec (not noexec). This is a "  # noqa: S108 — /tmp is the in-sandbox Docker tmpfs path, not a host temp file
                "sandbox regression, not fixable by editing code."
            )
        return None
    if missing_bin:
        return (
            f"{ENV_ERROR_PREFIX} command not found in sandbox: "
            f"'{missing_bin}' (rc={rc}). This binary is not installed/on "
            "PATH; declare it via extra_sandbox_packages in "
            ".robotsix-mill/config.yaml (pip:<name> or apt:<name>) — not "
            "fixable by editing code."
        )
    return (
        f"{ENV_ERROR_PREFIX} a command was not found in sandbox "
        f"(rc={rc}). A binary referenced by the test command is not "
        "installed/on PATH; declare it via extra_sandbox_packages in "
        ".robotsix-mill/config.yaml (pip:<name> or apt:<name>) — not "
        "fixable by editing code."
    )


def is_network_dependent_failure(out: str) -> bool:
    """Return ``True`` when *out* (raw test log or diag) shows a
    sandbox-hostile network signature — no real assertion failure.

    Matches: connection refused/reset, DNS/name-resolution failures,
    and the empty-response ``JSONDecodeError`` (``char 0`` form only).

    Conservative: a normal ``AssertionError`` or a ``JSONDecodeError``
    on malformed (non-empty) data never matches.
    """
    # Connection refused / reset — httpx/requests/aiohttp signatures.
    if re.search(
        r"(?:ConnectError|ConnectionError|ConnectionRefused|"
        r"RemoteDisconnected|Max retries exceeded)",
        out,
        re.IGNORECASE,
    ):
        return True

    # DNS / name-resolution failures (system-level + requests).
    if re.search(
        r"(?:Name or service not known|nodename nor servname|"
        r"getaddrinfo|Temporary failure in name resolution|"
        r"Failed to resolve)",
        out,
        re.IGNORECASE,
    ):
        return True

    # Empty-response JSONDecodeError: the hallmark of an HTTP call that
    # received an empty body in the sandbox (network blocked / proxy
    # returning empty).  The "char 0" form means s == '' (empty string);
    # a JSONDecodeError on non-empty malformed data (e.g. a fixture
    # with a stray brace) has a non-zero column — we do NOT match those.
    if re.search(
        r"JSONDecodeError[^)]*Expecting value: line 1 column 1 \(char 0\)",
        out,
        re.IGNORECASE,
    ):
        return True

    return False


def _load_file_map(repo_dir: Path) -> list[str] | None:
    """Load the file_map from ``artifacts/file_map.json``, or ``None``."""
    fm_path = repo_dir.parent / "artifacts" / "file_map.json"
    if not fm_path.exists():
        return None
    try:
        raw = json.loads(fm_path.read_text(encoding="utf-8"))
        if not raw:
            return None
        return [entry["file"] for entry in raw]
    except json.JSONDecodeError, KeyError, OSError:
        return None


def _check_pyproject_toml(repo_dir: Path) -> str | None:
    """Return an error message if ``pyproject.toml`` is missing or invalid TOML, else ``None``.

    A fast deterministic gate that catches broken TOML before the sandbox
    is invoked — ``pip install .`` and downstream tools (ruff, pytest)
    both fail with a cryptic parse error, and a pre-flight check returns
    a clear diagnostic instead.
    """
    toml_path = repo_dir / "pyproject.toml"
    if not toml_path.exists():
        return None
    try:
        tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"pyproject.toml is invalid TOML: {exc}"
    return None


def _evaluate_gate_result(
    *,
    settings: Settings,
    repo_dir: Path,
    cmd: str,
    rc: int,
    out: str,
    retry_on_failure: bool,
    sandbox_image: str | None,
    file_map: list[str] | None,
    success_msg: str,
    retry_success_msg: str,
    is_test_gate: bool = False,
) -> tuple[bool, str]:
    """Evaluate the result of a gate command (test or smoke).

    Handles the common post-run logic: rc==0 pass, rc==5 no-tests
    (test gate only), environmental errors, flake retry, and
    distillation.
    """
    if rc == 0:
        return True, success_msg

    # pytest exits 5 when it collects ZERO tests ("no tests ran"). A suite
    # with no tests yet is not a regression — most importantly, it must NOT
    # poison the baseline check of a freshly-scaffolded repo (which ships an
    # empty tests/ dir) and block every ticket on its board. Treat the
    # pytest no-tests signal as passing.
    if is_test_gate and rc == 5 and "no tests ran" in out.lower():
        return True, "no tests collected (pytest rc=5) — treated as passing"

    # Environmental failure: a binary referenced by the gate command is not
    # installed / not on PATH (``rc=127`` or an explicit command-not-found
    # signature). This is NOT fixable by editing code, so skip the distill
    # LLM and return a STABLE, byte-identical diagnosis carrying a fixed
    # marker and the binary name. The stability is load-bearing: the
    # implement fix-loop circuit breaker fires when the same diagnosis
    # repeats, capping unfixable env failures instead of burning every
    # fix iteration. Conservative by design — only rc 127 or a real
    # command-not-found signature triggers this; a normal assertion
    # failure (rc 1) still flows to the distill agent below.
    env_diag = _env_error_diag(rc, out)
    if env_diag is not None:
        return False, env_diag

    if retry_on_failure:
        from .. import sandbox as _sandbox

        try:
            rc2, out2 = _sandbox.run(
                cmd,
                repo_dir=repo_dir,
                settings=settings,
                install_project=True,
                sandbox_image=sandbox_image,
            )
        except _sandbox.SandboxError as e:
            return False, f"sandbox unavailable on flake re-run: {e}"
        if rc2 == 0:
            return True, retry_success_msg
        # Both runs red — distill the SECOND output (fresher, and the
        # one a fix ticket will be written against).
        rc, out = rc2, out2

    return False, _distill_failure(settings, repo_dir, rc, out, file_map=file_map)


def _run_gate_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    repo_config: RepoConfig | None = None,
    retry_on_failure: bool = False,
    file_map: list[str] | None = None,
    cmd: str,
    success_msg: str,
    gate_name: str,
    is_test_gate: bool = False,
) -> tuple[bool, str]:
    """Shared gate-runner for test and smoke agents.

    Resolves the file_map, runs *cmd* in the sandbox (with
    ``install_project=True``), and delegates to
    :func:`_evaluate_gate_result`.  *cmd* must already be resolved
    and stripped; callers handle empty-command short-circuit.
    """
    if file_map is None:
        file_map = _load_file_map(repo_dir)

    from .. import sandbox

    toml_err = _check_pyproject_toml(repo_dir)
    if toml_err is not None:
        return False, toml_err

    image = repo_config.sandbox_image if repo_config else None
    try:
        # install_project: install the repo's DECLARED deps before the
        # gate runs. Without this the gate tests against the image's
        # frozen site-packages, so any ticket adding a new third-party
        # runtime dep fails forever with ModuleNotFoundError.
        rc, out = sandbox.run(
            cmd,
            repo_dir=repo_dir,
            settings=settings,
            install_project=True,
            sandbox_image=image,
        )
    except sandbox.SandboxError as e:
        return False, f"sandbox unavailable: {e}"

    retry_success_msg = (
        f"{gate_name} passed on re-run (first run failed rc={rc} — flaky); "
        "treated as passing"
    )

    return _evaluate_gate_result(
        settings=settings,
        repo_dir=repo_dir,
        cmd=cmd,
        rc=rc,
        out=out,
        retry_on_failure=retry_on_failure,
        sandbox_image=image,
        file_map=file_map,
        success_msg=success_msg,
        retry_success_msg=retry_success_msg,
        is_test_gate=is_test_gate,
    )


def run_test_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    repo_config: RepoConfig | None = None,
    retry_on_failure: bool = False,
    file_map: list[str] | None = None,
) -> tuple[bool, str]:
    """Run the test command in the sandbox. Return ``(passed,
    feedback)``. On pass, feedback is a short confirmation; on fail it
    is a cheap-model distilled, actionable diagnosis (NOT the raw log).
    Sandbox infra failure -> ``(False, "<reason>")`` so the coordinator
    can react.

    ``retry_on_failure``: re-run the suite ONCE before distilling a
    failure; a green re-run is reported as passing (flaky first run).
    The baseline gate sets this — one flaky test on main otherwise
    fabricates "pre-existing test failures on main", blocks the ticket
    AND spawns a bogus dependency-fix ticket (live case: a74b blocked on
    a test that passed when the distill agent re-ran it minutes later).
    The implement fix loop leaves it off: there the suite re-runs next
    iteration anyway, and doubling every red gate run would be pure
    cost.

    ``file_map``: an optional list of file paths the implement agent is
    permitted to change. When ``None``, auto-discovered from
    ``repo_dir.parent / "artifacts" / "file_map.json"`` (the sibling
    convention of :class:`~robotsix_mill.core.workspace.Workspace`).
    Injected into the distill sub-agent's user message as a soft scope
    hint so the cheap model prefers fixes within declared scope —
    avoiding wasted implement iterations that the scope guardrail would
    REJECT. No effect when the test passes or when the failure is an
    ENV-ERROR (handled deterministically without the distill LLM).

    Test command resolution (highest precedence first): the per-repo
    ``.robotsix-mill/config.yaml`` ``test_command`` committed in the
    clone wins when set (a managed repo owns its command), else
    ``settings.test_command`` (the fleet-wide global fallback). When both
    are empty the gate short-circuits to PASS — repos without a test
    suite (doc-only, etc.) need no opt-out flag. (``repo_config`` no
    longer carries a per-repo ``test_command``; it moved to the repo's
    own ``.robotsix-mill/config.yaml``.)"""
    cmd = ((load_repo_test_command(repo_dir) or "") or settings.test_command).strip()
    if not cmd:
        return True, "no test gate configured (treated as passing)"

    return _run_gate_agent(
        settings=settings,
        repo_dir=repo_dir,
        repo_config=repo_config,
        retry_on_failure=retry_on_failure,
        file_map=file_map,
        cmd=cmd,
        success_msg="all tests passed",
        gate_name="tests",
        is_test_gate=True,
    )


def smoke_paths_match(changed_files: list[str], smoke_paths: list[str]) -> bool:
    """Return ``True`` when the smoke gate should run for *changed_files*.

    Pure and side-effect-free (no sandbox / git) so it is unit-testable
    in isolation. An empty ``smoke_paths`` means "run unconditionally"
    (the gate is path-scoped only when globs are declared). Otherwise the
    gate runs when ANY changed file matches ANY glob.

    Matching uses both :func:`fnmatch.fnmatch` and
    :meth:`pathlib.PurePath.match` against POSIX-style relative paths (as
    returned by ``git_ops.introduced_files``) so directory-recursive
    patterns like ``src/robotsix_mill/runtime/**`` and shallow patterns
    like ``src/robotsix_mill/runtime/static/*.css`` both work."""
    if not smoke_paths:
        return True
    for path in changed_files:
        pure = PurePath(path)
        for pattern in smoke_paths:
            if fnmatch.fnmatch(path, pattern):
                return True
            try:
                if pure.match(pattern):
                    return True
            except ValueError:
                # An invalid pattern for PurePath.match — fnmatch already
                # had its chance above; treat as non-matching.
                continue
    return False


def run_smoke_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    repo_config: RepoConfig | None = None,
    retry_on_failure: bool = False,
    file_map: list[str] | None = None,
) -> tuple[bool, str]:
    """Run the smoke command in the sandbox. Return ``(passed,
    feedback)``. Closely mirrors :func:`run_test_agent`: on pass the
    feedback is a short confirmation; on fail it is a cheap-model
    distilled, actionable diagnosis (NOT the raw log). Sandbox infra
    failure -> ``(False, "<reason>")`` so the coordinator can react.

    Smoke command resolution (highest precedence first): the per-repo
    ``.robotsix-mill/config.yaml`` ``smoke_command`` committed in the
    clone wins when set, else ``settings.smoke_command`` (the fleet-wide
    global fallback). When both are empty the gate short-circuits to
    PASS — the smoke gate is strictly opt-in, so a repo without a smoke
    command no-ops.

    ``retry_on_failure``: re-run the smoke command ONCE before distilling
    a failure (mirrors the test-gate flake guard).

    ``file_map``: mirrors :func:`run_test_agent` — auto-discovered from
    the sibling ``artifacts/file_map.json`` when ``None``."""
    cmd = ((load_repo_smoke_command(repo_dir) or "") or settings.smoke_command).strip()
    if not cmd:
        return True, "no smoke gate configured (treated as passing)"

    return _run_gate_agent(
        settings=settings,
        repo_dir=repo_dir,
        repo_config=repo_config,
        retry_on_failure=retry_on_failure,
        file_map=file_map,
        cmd=cmd,
        success_msg="smoke passed",
        gate_name="smoke",
        is_test_gate=False,
    )


def _distill_failure(
    settings: Settings,
    repo_dir: Path,
    rc: int,
    out: str,
    *,
    file_map: list[str] | None = None,
) -> str:
    """Distill a raw failing-test log into a short, actionable diagnosis
    via a CHEAP model. Degrades to the raw tail when no model key is set
    or the distill agent errors."""
    tail = out[-6000:]

    scope_note = ""
    if file_map:
        file_list = "\n".join(f"  - {f}" for f in file_map)
        scope_note = (
            f"\n\nDeclared file scope (prefer fixes within these files):\n{file_list}"
        )

    if not get_secrets().openrouter_api_key:
        return f"tests failed (rc={rc}); raw tail:\n{tail[-1500:]}"

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import run_agent

    from pydantic_ai.usage import UsageLimits

    from ..data_paths import data_dir

    definition = load_agent_definition(data_dir("agent_definitions") / "tester.yaml")

    all_fs = build_fs_tools(repo_dir, settings)
    ro_fs_tools = [
        t for t in all_fs if t.__name__ in ("read_file", "list_dir", "run_command")
    ]
    explore_tool = make_explore_tool(settings, repo_dir)

    agent = build_agent_from_definition(
        settings,
        definition,
        repo_dir=repo_dir,  # confine the SDK's built-in Bash/Read to the clone
        tools=[*ro_fs_tools, explore_tool],
    )
    limits = UsageLimits(request_limit=settings.test_request_limit)
    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(
                f"<test_output rc={rc}>\n{tail}\n</test_output>{scope_note}",
                usage_limits=limits,
            ),
            what="test-distill",
        )
        return str(result.output).strip()
    except Exception as e:  # noqa: BLE001 — degrade to raw tail
        return f"tests failed (rc={rc}); distill error {e}:\n{tail[-1500:]}"
    finally:
        _safe_close(agent)
