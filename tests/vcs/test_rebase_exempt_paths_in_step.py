"""The rebase-drop exempt list is duplicated; keep the copies in step.

``git_ops.DEFAULT_REBASE_DROP_EXEMPT_PATHS`` documents the default, but the
merge stage passes ``settings.rebase_drop_exempt_paths`` — so editing only
the constant changes nothing at runtime. That is an easy mistake to make:
the constant is the one you find by grepping for the path names.

Importing the constant into the settings module would remove the
duplication, but ``config`` importing ``vcs`` creates a cycle. A test is
the cheaper guard.
"""

from __future__ import annotations

from robotsix_mill.config import Settings
from robotsix_mill.vcs.git_ops import DEFAULT_REBASE_DROP_EXEMPT_PATHS


def test_settings_default_matches_the_git_ops_constant() -> None:
    assert sorted(Settings().rebase_drop_exempt_paths) == sorted(
        DEFAULT_REBASE_DROP_EXEMPT_PATHS
    ), (
        "rebase_drop_exempt_paths and DEFAULT_REBASE_DROP_EXEMPT_PATHS have "
        "drifted. The merge stage passes the settings list, so update that "
        "one too — changing only the constant has no runtime effect."
    )


def test_generated_registry_files_are_exempt() -> None:
    """Files whose content is a function of the whole tree, not one branch.

    A rebase can legitimately land a version matching neither side, which
    the guard's blob-equality excuse cannot clear — so they must be exempt
    or every rebase touching them reports a false drop.
    """
    exempt = set(Settings().rebase_drop_exempt_paths)
    for path in ("CHANGELOG.md", "docs/modules.yaml", ".secrets.baseline"):
        assert path in exempt, f"{path} is generated and must be exempt"
