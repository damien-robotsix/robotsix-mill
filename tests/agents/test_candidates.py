"""Parser + status-mutator tests for AGENT_CANDIDATES.md."""

from __future__ import annotations

from pathlib import Path

from robotsix_mill.agents.candidates import (
    Candidate,
    candidates_path,
    load_candidates,
    prune_candidates,
    to_ticket_payload,
    update_status,
)


_BLOCK1 = """\
### Proposed addition to ## Project layout

> **Rule:** New CLI subcommands live in `src/<pkg>/cli/`; the
> entrypoint `cli.py` only routes.

**Rationale:** observed across tickets `aaa`, `bbb`.

**Proposed:** 2026-05-30 11:00 UTC (from 20260530T110000Z-some-ticket-aaaa)

---
"""

_BLOCK2 = """\
### Proposed addition to ## Testing conventions

> **Rule:** Each new module needs at least one black-box test
> exercising its public API.

**Rationale:** missing-tests pattern in tickets `ccc`, `ddd`, `eee`.

**Proposed:** 2026-05-30 12:00 UTC (from 20260530T120000Z-other-ticket-bbbb)

---
"""


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "AGENT_CANDIDATES.md"
    p.write_text(content)
    return p


def test_load_empty_file_returns_empty_list(tmp_path):
    p = _write(tmp_path, "")
    assert load_candidates(p) == []


def test_load_missing_file_returns_empty_list(tmp_path):
    assert load_candidates(tmp_path / "does_not_exist.md") == []


def test_load_single_block_parses_fields(tmp_path):
    p = _write(tmp_path, _BLOCK1)
    out = load_candidates(p)
    assert len(out) == 1
    c = out[0]
    assert c.section == "## Project layout"
    assert "New CLI subcommands live in" in c.rule
    assert c.rationale.startswith("observed across tickets")
    assert c.proposed_at == "2026-05-30 11:00 UTC"
    assert c.source_ticket == "20260530T110000Z-some-ticket-aaaa"
    assert c.status == "pending"
    assert c.filed_ticket is None
    assert len(c.candidate_id) == 8


def test_load_two_blocks_parses_both(tmp_path):
    p = _write(tmp_path, _BLOCK1 + "\n" + _BLOCK2)
    out = load_candidates(p)
    assert len(out) == 2
    sections = [c.section for c in out]
    assert "## Project layout" in sections
    assert "## Testing conventions" in sections
    # Distinct stable IDs.
    assert out[0].candidate_id != out[1].candidate_id


def test_load_skips_malformed_blocks(tmp_path):
    """A block missing a required field is dropped, not crash."""
    malformed = "### Proposed addition to ## Bad\n\n(no rule, no rationale, no proposed)\n\n---\n"
    p = _write(tmp_path, malformed + "\n" + _BLOCK1)
    out = load_candidates(p)
    assert len(out) == 1
    assert out[0].section == "## Project layout"


def test_candidate_id_stable_across_loads(tmp_path):
    """The hash-based id stays the same when the file is re-read."""
    p = _write(tmp_path, _BLOCK1)
    id1 = load_candidates(p)[0].candidate_id
    id2 = load_candidates(p)[0].candidate_id
    assert id1 == id2


def test_candidate_id_stable_across_reordering(tmp_path):
    """Block order doesn't shift IDs — they're content-hashed."""
    p1 = _write(tmp_path, _BLOCK1 + "\n" + _BLOCK2)
    ids_a = sorted(c.candidate_id for c in load_candidates(p1))
    # Same content, swapped order.
    p2 = _write(tmp_path, _BLOCK2 + "\n" + _BLOCK1)
    ids_b = sorted(c.candidate_id for c in load_candidates(p2))
    assert ids_a == ids_b


def test_update_status_validate_stamps_block(tmp_path):
    p = _write(tmp_path, _BLOCK1)
    cid = load_candidates(p)[0].candidate_id
    updated = update_status(p, cid, "validated", filed_ticket="20260531-mill-123")
    assert updated is not None
    assert updated.status == "validated"
    assert updated.filed_ticket == "20260531-mill-123"

    # Re-read confirms persistence.
    again = load_candidates(p)[0]
    assert again.status == "validated"
    assert again.filed_ticket == "20260531-mill-123"

    # The Status line is present in the file.
    text = p.read_text()
    assert "**Status:** validated → 20260531-mill-123" in text


def test_update_status_reject_no_ticket(tmp_path):
    p = _write(tmp_path, _BLOCK1)
    cid = load_candidates(p)[0].candidate_id
    updated = update_status(p, cid, "rejected")
    assert updated is not None
    assert updated.status == "rejected"
    assert updated.filed_ticket is None
    text = p.read_text()
    assert "**Status:** rejected" in text
    assert "→" not in text.split("**Status:**", 1)[1].split("\n", 1)[0]


def test_update_status_only_target_block_changes(tmp_path):
    """Stamping one block doesn't touch the other."""
    p = _write(tmp_path, _BLOCK1 + "\n" + _BLOCK2)
    cands = load_candidates(p)
    cid_block2 = next(
        c for c in cands if c.section == "## Testing conventions"
    ).candidate_id
    update_status(p, cid_block2, "rejected")
    after = load_candidates(p)
    block1 = next(c for c in after if c.section == "## Project layout")
    block2 = next(c for c in after if c.section == "## Testing conventions")
    assert block1.status == "pending"
    assert block2.status == "rejected"


def test_update_status_unknown_id_returns_none(tmp_path):
    p = _write(tmp_path, _BLOCK1)
    assert update_status(p, "deadbeef", "validated", filed_ticket="X") is None


def test_update_status_replaces_existing_status_line(tmp_path):
    """A second validate doesn't pile up Status lines — it replaces."""
    p = _write(tmp_path, _BLOCK1)
    cid = load_candidates(p)[0].candidate_id
    update_status(p, cid, "validated", filed_ticket="ticket-A")
    update_status(p, cid, "rejected")  # reconsidered
    text = p.read_text()
    # Exactly one Status line, with the second value.
    assert text.count("**Status:**") == 1
    assert "**Status:** rejected" in text


def test_to_ticket_payload_renders_audited_repo_body():
    c = Candidate(
        candidate_id="abcd1234",
        section="## Project layout",
        rule="New CLI subcommands live in `src/<pkg>/cli/`.",
        rationale="observed across multiple tickets",
        proposed_at="2026-05-30 11:00 UTC",
        source_ticket="20260530T110000Z-some-ticket-aaaa",
        status="pending",
        filed_ticket=None,
    )
    title, body = to_ticket_payload(c)
    assert title.startswith("AGENT.md:")
    assert "New CLI subcommands" in title
    assert "## Project layout" in body
    assert "Edit `AGENT.md`" in body
    assert "abcd1234" in body
    assert "20260530T110000Z-some-ticket-aaaa" in body


def test_candidates_path_per_board():
    base = Path("/data")
    assert candidates_path(base, "robotsix-mill") == Path(
        "/data/robotsix-mill/AGENT_CANDIDATES.md"
    )
    assert candidates_path(base, "") == Path("/data/AGENT_CANDIDATES.md")


# ---------------------------------------------------------------------------
#  prune_candidates — bounded retention
# ---------------------------------------------------------------------------


def _block(idx: int, status: str | None = None) -> str:
    """Build a well-formed candidate block. ``idx`` keeps each block's
    rule/proposed-at distinct so candidate_ids differ. When *status* is
    given a ``**Status:**`` line is appended (a resolved block)."""
    status_line = ""
    if status is not None:
        filed = f" → 20260601-mill-{idx:03d}" if status == "validated" else ""
        status_line = f"**Status:** {status}{filed}\n\n"
    return (
        f"### Proposed addition to ## Section {idx}\n\n"
        f"> **Rule:** Rule number {idx} body text.\n\n"
        f"**Rationale:** rationale for block {idx}.\n\n"
        f"**Proposed:** 2026-05-30 1{idx}:00 UTC (from 20260530T1{idx}0000Z-tkt-{idx:04d})\n\n"
        f"{status_line}"
        f"---\n"
    )


def test_prune_noop_when_max_entries_zero(tmp_path):
    content = _block(1, "validated") + "\n" + _block(2)
    p = _write(tmp_path, content)
    assert prune_candidates(p, 0) == 0
    assert p.read_text() == content


def test_prune_noop_when_file_missing(tmp_path):
    missing = tmp_path / "nope.md"
    assert prune_candidates(missing, 10) == 0
    assert not missing.exists()


def test_prune_drops_oldest_resolved_keeps_pending(tmp_path):
    # 3 resolved (oldest first) + 2 pending, cap 3.
    content = (
        _block(1, "validated")
        + "\n"
        + _block(2, "rejected")
        + "\n"
        + _block(3, "validated")
        + "\n"
        + _block(4)
        + "\n"
        + _block(5)
    )
    p = _write(tmp_path, content)
    dropped = prune_candidates(p, 3)
    # 2 pending always kept → only 1 resolved slot → drop 2 oldest resolved.
    assert dropped == 2
    out = load_candidates(p)
    assert len(out) == 3
    statuses = [c.status for c in out]
    assert statuses.count("pending") == 2
    # The single surviving resolved block is the most-recent one (block 3).
    surviving_resolved = [c for c in out if c.status != "pending"]
    assert len(surviving_resolved) == 1
    assert surviving_resolved[0].section == "## Section 3"
    # Raw content of a survivor is preserved verbatim.
    text = p.read_text()
    assert "Rule number 3 body text." in text
    assert "Rule number 1 body text." not in text


def test_prune_pending_exceeds_cap_keeps_all_pending(tmp_path, caplog):
    import logging

    # 3 pending + 2 resolved, cap 2 → all pending kept, all resolved dropped.
    content = (
        _block(1, "validated")
        + "\n"
        + _block(2)
        + "\n"
        + _block(3)
        + "\n"
        + _block(4)
        + "\n"
        + _block(5, "rejected")
    )
    p = _write(tmp_path, content)
    with caplog.at_level(logging.WARNING):
        dropped = prune_candidates(p, 2)
    assert dropped == 2  # both resolved blocks dropped
    out = load_candidates(p)
    assert len(out) == 3
    assert all(c.status == "pending" for c in out)
    assert any("pending backlog" in r.message for r in caplog.records)


def test_prune_atomic_no_tmp_left_and_valid(tmp_path):
    content = _block(1, "validated") + "\n" + _block(2, "rejected") + "\n" + _block(3)
    p = _write(tmp_path, content)
    prune_candidates(p, 1)
    # No stray temp file remains.
    assert not (tmp_path / "AGENT_CANDIDATES.md.tmp").exists()
    # File still round-trips.
    out = load_candidates(p)
    assert len(out) == 1
    assert out[0].status == "pending"


def test_prune_returns_zero_when_nothing_to_drop(tmp_path):
    content = _block(1, "validated") + "\n" + _block(2)
    p = _write(tmp_path, content)
    before = p.read_text()
    assert prune_candidates(p, 10) == 0
    assert p.read_text() == before
