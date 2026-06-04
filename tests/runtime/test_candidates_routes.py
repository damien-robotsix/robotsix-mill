"""HTTP-level tests for the AGENT.md candidate routes.

Covers GET /candidates, POST /candidates/{id}/validate, and POST
/candidates/{id}/reject — the parser unit tests live in
tests/agents/test_candidates.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.runtime.api import create_app


_BLOCK = """\
### Proposed addition to ## Project layout

> **Rule:** New CLI subcommands live in `src/<pkg>/cli/`.

**Rationale:** observed across tickets `aaa`, `bbb`.

**Proposed:** 2026-05-30 11:00 UTC (from 20260530T110000Z-some-ticket-aaaa)

---
"""

_BLOCK2 = """\
### Proposed addition to ## Testing conventions

> **Rule:** Every new module has at least one black-box test.

**Rationale:** missing-tests pattern in `ccc`, `ddd`.

**Proposed:** 2026-05-30 12:00 UTC (from 20260530T120000Z-other-ticket-bbbb)

---
"""


@pytest.fixture
def client(settings, repos_registry):
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


@pytest.fixture
def candidates_file(settings):
    """Write the file at the path retrospect would use (the board dir)."""
    p = settings.data_dir / "test-board" / "AGENT_CANDIDATES.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_BLOCK + "\n" + _BLOCK2)
    return p


def test_list_candidates_empty_when_file_missing(client):
    r = client.get("/candidates?repo_id=test-repo")
    assert r.status_code == 200
    assert r.json() == []


def test_list_candidates_returns_pending(client, candidates_file):
    r = client.get("/candidates?repo_id=test-repo")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    sections = sorted(c["section"] for c in data)
    assert sections == ["## Project layout", "## Testing conventions"]
    for c in data:
        assert c["status"] == "pending"
        assert c["filed_ticket"] is None
        assert len(c["candidate_id"]) == 8


def test_list_candidates_unknown_repo_400(client):
    r = client.get("/candidates?repo_id=unknown-repo")
    assert r.status_code == 400


def test_validate_files_audited_repo_ticket(client, candidates_file, service):
    r = client.get("/candidates?repo_id=test-repo")
    cid = r.json()[0]["candidate_id"]
    section = r.json()[0]["section"]

    v = client.post(f"/candidates/{cid}/validate?repo_id=test-repo")
    assert v.status_code == 200, v.text
    data = v.json()
    assert data["status"] == "validated"
    assert data["filed_ticket"], "expected a filed ticket id"

    # The ticket exists on the audited repo's board.
    ticket_id = data["filed_ticket"]
    fetched = service.get(ticket_id)
    assert fetched is not None
    assert "AGENT.md" in fetched.title
    # The status is persisted — re-fetching the list with the default
    # filter (pending only) no longer surfaces this candidate.
    again = client.get("/candidates?repo_id=test-repo").json()
    assert all(c["candidate_id"] != cid for c in again)
    # But include_acted=true does.
    full = client.get("/candidates?repo_id=test-repo&include_acted=true").json()
    survivor = next(c for c in full if c["candidate_id"] == cid)
    assert survivor["status"] == "validated"
    assert survivor["filed_ticket"] == ticket_id
    # section unchanged across the trip
    assert survivor["section"] == section


def test_validate_twice_returns_409(client, candidates_file):
    cid = client.get("/candidates?repo_id=test-repo").json()[0]["candidate_id"]
    client.post(f"/candidates/{cid}/validate?repo_id=test-repo")
    r = client.post(f"/candidates/{cid}/validate?repo_id=test-repo")
    assert r.status_code == 409
    assert "validated" in r.text.lower()


def test_validate_unknown_candidate_404(client, candidates_file):
    r = client.post("/candidates/deadbeef/validate?repo_id=test-repo")
    assert r.status_code == 404


def test_reject_marks_status_no_ticket(client, candidates_file, service):
    cid = client.get("/candidates?repo_id=test-repo").json()[0]["candidate_id"]
    pre_count = len(service.list())

    r = client.post(f"/candidates/{cid}/reject?repo_id=test-repo")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "rejected"
    assert data["filed_ticket"] is None

    # No ticket was filed.
    assert len(service.list()) == pre_count

    # Default list filters it out.
    pending = client.get("/candidates?repo_id=test-repo").json()
    assert all(c["candidate_id"] != cid for c in pending)


def test_reject_unknown_candidate_404(client, candidates_file):
    r = client.post("/candidates/deadbeef/reject?repo_id=test-repo")
    assert r.status_code == 404


def test_only_other_candidate_remains_pending_after_validate(client, candidates_file):
    """Two-candidate file: validating one leaves the other untouched."""
    listing = client.get("/candidates?repo_id=test-repo").json()
    assert len(listing) == 2
    cid_a = listing[0]["candidate_id"]

    v = client.post(f"/candidates/{cid_a}/validate?repo_id=test-repo")
    assert v.status_code == 200

    pending = client.get("/candidates?repo_id=test-repo").json()
    assert len(pending) == 1
    assert pending[0]["candidate_id"] != cid_a
