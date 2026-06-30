"""Static-asset assertions for the mill board stylesheet.

The mill board layers `board-mill.css` over the external robotsix-board
library, which renders a per-card move-to control
(`<form class="board-card-move">`). Mill columns are agent-driven
pipeline stages, so that manual control is suppressed via a CSS override
in `board-mill.css`. This test guards that the hide rule stays present.
"""

from __future__ import annotations

import pathlib

BOARD_MILL_CSS = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src/robotsix_mill/runtime/static/board-mill.css"
)


def _normalize(text: str) -> str:
    """Collapse all runs of whitespace to single spaces."""
    return " ".join(text.split())


def test_board_mill_css_hides_move_control() -> None:
    """`board-mill.css` hides robotsix-board's per-card move form."""
    css = _normalize(BOARD_MILL_CSS.read_text(encoding="utf-8"))
    assert ".board-card-move" in css
    assert ".board-card-move { display: none; }" in css
