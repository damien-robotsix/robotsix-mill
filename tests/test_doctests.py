"""Run doctests for modules that use them as executable documentation.

The ``--doctest-modules`` flag in ``addopts`` only applies to modules
discovered via ``testpaths``, which is ``["tests"]``.  Source modules
under ``src/`` are excluded from test discovery, so their doctests
must be triggered explicitly.
"""

import doctest


def test_datetime_utils_doctests() -> None:
    import robotsix_mill.core.datetime_utils

    results = doctest.testmod(robotsix_mill.core.datetime_utils)
    assert results.failed == 0, f"{results.failed} doctest failure(s)"


def test_review_doctests() -> None:
    import robotsix_mill.stages.review

    results = doctest.testmod(robotsix_mill.stages.review)
    assert results.failed == 0, f"{results.failed} doctest failure(s)"
