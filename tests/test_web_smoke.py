import pytest
from fastapi.testclient import TestClient

from srotaai.web import app as web


@pytest.fixture()
def client(seed_db, monkeypatch):
    monkeypatch.setattr(web, "DB_PATH", seed_db)
    # don't start the background scheduler / network ingestion in tests
    monkeypatch.setattr(web.SrotaScheduler, "get",
                        classmethod(lambda cls, *_a, **_k: type("S", (), {
                            "start": lambda s: None, "stop": lambda s: None})()))
    with TestClient(web.app) as c:
        yield c


@pytest.mark.parametrize("path", [
    "/", "/_ping", "/projects", "/projects/new", "/signals", "/compliance",
    "/sources", "/about", "/search?q=metformin",
    "/projects/pv-india-otc", "/projects/pv-india-otc/records.csv",
])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_unknown_project_is_404(client):
    assert client.get("/projects/does-not-exist").status_code == 404


def test_add_source_rejects_internal_urls(client):
    r = client.post("/projects/pv-india-otc/sources/add",
                    data={"url": "http://169.254.169.254/", "name": "evil"},
                    follow_redirects=False)
    assert r.status_code == 400


def test_compliance_page_reports_intact_chain(client):
    body = client.get("/compliance").text.lower()
    assert "broken" not in body
