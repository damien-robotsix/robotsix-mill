"""Error-recovery checkpoint persistence for the refine stage.

Saves / loads / clears a ``refine_checkpoint.json`` artifact so a
resume-from-BLOCKED can skip re-running the expensive refine agent.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from ...agents import refining
from ...core.workspace import Workspace
from .helpers import log


def save_refine_checkpoint(
    ws: Workspace,
    result: refining.RefineResult,
) -> None:
    """Persist essential ``RefineResult`` fields so a resume-from-BLOCKED
    can skip re-running the expensive refine agent.

    The conversation state is already saved separately by
    :func:`save_conversation_state` for the pause mechanism; this
    checkpoint captures the structured output fields needed to
    reconstruct a ``RefineResult`` without calling the agent again.
    """
    children_data = None
    if result.children:
        children_data = [
            {
                "title": c.title,
                "spec_markdown": c.spec_markdown,
                "depends_on": c.depends_on,
            }
            for c in result.children
        ]
    file_map_data = None
    if result.file_map:
        file_map_data = [{"file": e.file, "note": e.note} for e in result.file_map]
    data: dict[str, Any] = {
        "spec_markdown": result.spec_markdown,
        "split": result.split,
        "children": children_data,
        "promote_to_epic": result.promote_to_epic,
        "epic_body": result.epic_body,
        "updated_memory": result.updated_memory,
        "file_map": file_map_data,
        "title": result.title,
        "reference_files": result.reference_files or [],
        "conversation_state_b64": (
            base64.b64encode(result.conversation_state).decode("ascii")
            if result.conversation_state
            else None
        ),
    }
    (ws.artifacts_dir / "refine_checkpoint.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def load_refine_checkpoint(
    ws: Workspace,
) -> tuple[refining.RefineResult | None, bytes | None]:
    """Load a saved refine error-recovery checkpoint.

    Returns ``(RefineResult, conversation_state_bytes)`` or
    ``(None, None)`` when no checkpoint exists.
    """
    path = ws.artifacts_dir / "refine_checkpoint.json"
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError, KeyError:
        log.warning("refine checkpoint corrupt — ignoring")
        return None, None

    conv_state: bytes | None = None
    if data.get("conversation_state_b64"):
        try:
            conv_state = base64.b64decode(data["conversation_state_b64"])
        except Exception:
            conv_state = None

    children = None
    if data.get("children"):
        children = [
            refining.ChildSpec(
                title=c["title"],
                spec_markdown=c["spec_markdown"],
                depends_on=c.get("depends_on", []),
            )
            for c in data["children"]
        ]
    file_map = None
    if data.get("file_map"):
        file_map = [
            refining.FileMapEntry(file=e["file"], note=e["note"])
            for e in data["file_map"]
        ]

    result = refining.RefineResult(
        spec_markdown=data.get("spec_markdown"),
        split=data.get("split", False),
        children=children,
        promote_to_epic=data.get("promote_to_epic", False),
        epic_body=data.get("epic_body"),
        updated_memory=data.get("updated_memory", ""),
        file_map=file_map,
        title=data.get("title"),
        reference_files=data.get("reference_files", []),
        conversation_state=conv_state,
    )
    return result, conv_state


def clear_refine_checkpoint(ws: Workspace) -> None:
    """Remove the error-recovery checkpoint when refine completes."""
    path = ws.artifacts_dir / "refine_checkpoint.json"
    if path.exists():
        path.unlink()
