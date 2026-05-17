import pytest
from fastapi.testclient import TestClient

from robotsix_mill.runtime.api import create_app


@pytest.fixture
def client(settings):
    # TestClient runs the lifespan: init_db, worker start/stop.
    with TestClient(create_app(settings)) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_get(client):
    r = client.post("/tickets", json={"title": "T", "description": "body"})
    assert r.status_code == 201
    tid = r.json()["id"]

    got = client.get(f"/tickets/{tid}")
    assert got.status_code == 200
    assert got.json()["title"] == "T"

    desc = client.get(f"/tickets/{tid}/description").json()
    assert desc["description"] == "body"

    assert tid in [t["id"] for t in client.get("/tickets").json()]


def test_get_missing_404(client):
    assert client.get("/tickets/nope").status_code == 404


def test_illegal_transition_409(client):
    tid = client.post("/tickets", json={"title": "T"}).json()["id"]
    r = client.post(f"/tickets/{tid}/transition", json={"state": "done"})
    assert r.status_code == 409
