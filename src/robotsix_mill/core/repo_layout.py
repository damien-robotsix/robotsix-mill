"""Pure, import-light helpers for resolving repo-relative paths to
their ``src/<pkg>/`` namespace when the literal token does not exist.

The mill pipeline's agent tools and deterministic gates construct
existence checks using paths relative to the repo root (e.g.
``robotsix_llmio/core``, ``config/``, ``core/``) for packages that
actually live under the ``src/`` namespace (e.g.
``src/robotsix_llmio/core/``, ``src/robotsix_llmio/config/``).  A
literal probe misses, producing false "absent" results.  This module
provides a single shared resolution step consumed everywhere those
checks happen — agents, gates, and the draft-routing heuristic.

Imports are deliberately minimal (``pathlib`` only) so the module is
safe to import from ``core/``, ``agents/``, and ``stages/`` without
circular-import risk.
"""

from __future__ import annotations

import pathlib


def src_path_candidates(token: str) -> list[str]:
    """Ordered candidate repo-relative spellings for *token*.

    Returns the literal *token* first, then a ``src/``-prefixed form
    when the token is relative and not already under ``src/``.
    De-duplicated, order preserved.

    Rules:

    - A *token* that already starts with ``src/`` (case-insensitive)
      is returned as-is — the ``src/``-prefixed duplicate is skipped
      so callers never produce ``src/src/...``.
    - An absolute path (``/`` or ``\\``) is returned as-is — absolute
      paths are never rewritten.
    - A leading ``./`` is normalised away so ``./robotsix_llmio`` and
      ``robotsix_llmio`` behave identically.
    """
    # Normalise leading ./ if present.
    normalised = token
    if normalised.startswith("./"):
        normalised = normalised[2:]

    candidates: list[str] = [normalised]

    # Absolute paths are never rewritten.
    if pathlib.PurePosixPath(normalised).is_absolute():
        return candidates

    # Skip src/ prefix when token already starts with src/ (case-insensitive).
    if normalised.lower().startswith("src/"):
        return candidates

    src_form = "src/" + normalised
    if src_form not in candidates:
        candidates.append(src_form)

    return candidates


def resolve_under_src(repo_dir: pathlib.Path, token: str) -> pathlib.Path | None:
    """Return the first **existing** path for *token* within *repo_dir*.

    Tries :func:`src_path_candidates` in order and returns the first
    candidate for which ``(repo_dir / candidate).exists()`` is True
    (covers both files and directories).  Returns ``None`` when no
    candidate exists.

    Pure / read-only — must never raise.  Defensive outer ``except``
    wraps every operation so a surprising token value (e.g. bytes
    leaked into a str path) cannot propagate an exception up into
    its caller.
    """
    try:
        # Absolute tokens cannot be repo-relative — pathlib's join of
        # repo_dir / "/absolute" replaces the left operand entirely,
        # which would probe the real filesystem rather than the repo.
        normalised = token.lstrip("./") if token.startswith("./") else token
        if pathlib.PurePosixPath(normalised).is_absolute():
            return None
        for candidate in src_path_candidates(token):
            if (repo_dir / candidate).exists():
                return repo_dir / candidate
    except Exception:  # noqa: BLE001, S110 — defensively swallow everything
        pass
    return None
