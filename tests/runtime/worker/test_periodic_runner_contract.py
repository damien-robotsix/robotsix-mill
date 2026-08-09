"""Every registered periodic runner must match the dispatcher's call shape.

``_fire_periodic_pass`` invokes each registered runner as::

    runner_fn(session_id=..., repo_config=...)

A runner whose signature does not accept those raises ``TypeError`` on
every single invocation. Nothing goes red when that happens: the poll loop
catches the exception, logs it, and carries on, so the pass is silently
dead for as long as the mismatch survives.

That is exactly how ``credit_balance`` was wired to
``run_credit_balance_check(settings=None)`` — a function that accepts
neither argument — and never ran once. The pass-shaped wrapper existed the
whole time; the registry simply pointed at the wrong name.

These tests check the wiring statically so the next mismatch fails here
instead of disappearing into the log.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from robotsix_mill.runtime.worker.poll_loops import PollLoopsMixin

# The keyword arguments _fire_periodic_pass supplies to every runner.
_DISPATCHER_KWARGS = ("session_id", "repo_config")


def _registries() -> list[tuple[str, dict[str, str]]]:
    found = []
    for name in ("_SCHEDULE_ONLY_RUNNERS", "_CUSTOM_LLM_AGENT_RUNNERS"):
        reg = getattr(PollLoopsMixin, name, None)
        if reg:
            found.append((name, reg))
    return found


def _all_entries() -> list[tuple[str, str, str]]:
    return [
        (reg_name, pass_name, path)
        for reg_name, reg in _registries()
        for pass_name, path in sorted(reg.items())
    ]


def _resolve(path: str):
    mod_path, attr = path.rsplit(":", 1)
    return getattr(importlib.import_module(mod_path), attr)


def test_there_is_something_to_check():
    """Guard against the parametrisation silently collapsing to zero."""
    assert _all_entries()


@pytest.mark.parametrize(
    ("reg_name", "pass_name", "path"),
    _all_entries(),
    ids=[f"{r}:{n}" for r, n, _ in _all_entries()],
)
def test_registered_runner_resolves(reg_name, pass_name, path):
    """A typo in the dotted path is only discovered at fire time otherwise."""
    assert callable(_resolve(path)), (
        f"{reg_name}[{pass_name}] -> {path} is not callable"
    )


@pytest.mark.parametrize(
    ("reg_name", "pass_name", "path"),
    _all_entries(),
    ids=[f"{r}:{n}" for r, n, _ in _all_entries()],
)
def test_registered_runner_accepts_the_dispatcher_kwargs(reg_name, pass_name, path):
    fn = _resolve(path)
    params = inspect.signature(fn).parameters
    accepts_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )

    missing = [
        kw for kw in _DISPATCHER_KWARGS if kw not in params and not accepts_kwargs
    ]
    assert not missing, (
        f"{reg_name}[{pass_name!r}] -> {path} does not accept {missing}. "
        f"_fire_periodic_pass calls it as fn(session_id=..., repo_config=...), "
        f"so every run would raise TypeError and be swallowed by the poll "
        f"loop. Add the parameter or point the registry at a pass-shaped "
        f"wrapper."
    )


@pytest.mark.parametrize(
    ("reg_name", "pass_name", "path"),
    _all_entries(),
    ids=[f"{r}:{n}" for r, n, _ in _all_entries()],
)
def test_registered_runner_is_callable_with_only_those_kwargs(
    reg_name, pass_name, path
):
    """Any other parameter must have a default — the dispatcher passes no more."""
    fn = _resolve(path)
    try:
        inspect.signature(fn).bind(session_id="s", repo_config=None)
    except TypeError as exc:  # pragma: no cover - the assert carries the message
        pytest.fail(
            f"{reg_name}[{pass_name!r}] -> {path} cannot be called as the "
            f"dispatcher calls it: {exc}"
        )


def test_credit_balance_specifically_is_wired_to_the_pass_wrapper():
    """Regression: it pointed at run_credit_balance_check and never ran."""
    path = PollLoopsMixin._SCHEDULE_ONLY_RUNNERS["credit_balance"]
    assert path.endswith(":run_credit_balance_pass"), (
        "credit_balance must use the pass-shaped wrapper, not the bare check"
    )
    inspect.signature(_resolve(path)).bind(session_id="s", repo_config=None)
