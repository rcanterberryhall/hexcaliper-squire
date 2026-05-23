"""Integration tests for /lancellmot/* routes (parsival#43)."""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import app
import db
import lancellmot_client


client = TestClient(app)


# ── alias CRUD ───────────────────────────────────────────────────────────────

def test_get_aliases_returns_list():
    db.upsert_lancellmot_alias("Foo", "id-foo", "foo-name")
    db.upsert_lancellmot_alias("Bar", "id-bar", "bar-name")
    resp = client.get("/lancellmot/aliases")
    assert resp.status_code == 200
    body = resp.json()
    names = {a["parsival_project"] for a in body}
    assert {"Foo", "Bar"}.issubset(names)


def test_put_alias_creates_new():
    resp = client.put("/lancellmot/aliases", json={
        "parsival_project": "NewProj",
        "lancellmot_project_id": "id-new",
        "lancellmot_project_name": "new-name",
    })
    assert resp.status_code == 200
    row = db.get_lancellmot_alias_for_tag("NewProj")
    assert row["lancellmot_project_id"] == "id-new"


def test_put_alias_updates_existing():
    db.upsert_lancellmot_alias("Upd", "old-id", "old-name")
    resp = client.put("/lancellmot/aliases", json={
        "parsival_project": "Upd",
        "lancellmot_project_id": "new-id",
        "lancellmot_project_name": "new-name",
    })
    assert resp.status_code == 200
    row = db.get_lancellmot_alias_for_tag("Upd")
    assert row["lancellmot_project_id"] == "new-id"


def test_put_alias_missing_field_is_400():
    resp = client.put("/lancellmot/aliases", json={"parsival_project": "X"})
    assert resp.status_code == 400


def test_delete_alias_removes():
    db.upsert_lancellmot_alias("Gone", "id-g", "g-name")
    resp = client.delete("/lancellmot/aliases/Gone")
    assert resp.status_code == 200
    assert db.get_lancellmot_alias_for_tag("Gone") is None


# ── projects proxy ────────────────────────────────────────────────────────────

def test_get_lancellmot_projects_proxies_client():
    fake_projects = [{"id": "p1", "name": "alpha"}, {"id": "p2", "name": "beta"}]
    with patch("app.lancellmot_client.list_projects", return_value=fake_projects):
        resp = client.get("/lancellmot/projects")
    assert resp.status_code == 200
    assert resp.json() == fake_projects


def test_get_lancellmot_projects_503_on_unavailable():
    with patch("app.lancellmot_client.list_projects",
               side_effect=lancellmot_client.LancellmotUnavailable("boom")):
        resp = client.get("/lancellmot/projects")
    assert resp.status_code == 503
    assert resp.json() == {"error": "unreachable"}


# ── docs-for-tag (render path) ────────────────────────────────────────────────

def test_docs_for_tag_ok_path():
    db.upsert_lancellmot_alias("Alpha", "proj-a", "alpha-proj")
    fake_docs = [
        {"id": "d1", "filename": "spec.pdf"},
        {"id": "d2", "filename": "procedure.md"},
    ]
    with patch("app.lancellmot_client.list_documents",
               return_value=fake_docs) as mock_docs:
        resp = client.get("/lancellmot/docs-for-tag?tag=Alpha")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["project_name"] == "alpha-proj"
    assert body["project_id"] == "proj-a"
    assert body["docs"] == fake_docs
    mock_docs.assert_called_once_with("proj-a", limit=5)


def test_docs_for_tag_unmapped():
    resp = client.get("/lancellmot/docs-for-tag?tag=NoAlias")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unmapped"
    assert body["tag"] == "NoAlias"


def test_docs_for_tag_unreachable():
    db.upsert_lancellmot_alias("Beta", "proj-b", "beta-proj")
    with patch("app.lancellmot_client.list_documents",
               side_effect=lancellmot_client.LancellmotUnavailable("down")):
        resp = client.get("/lancellmot/docs-for-tag?tag=Beta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unreachable"
    assert body["tag"] == "Beta"


def test_docs_for_tag_respects_limit_param():
    db.upsert_lancellmot_alias("Gamma", "proj-g", "gamma-proj")
    with patch("app.lancellmot_client.list_documents",
               return_value=[]) as mock_docs:
        resp = client.get("/lancellmot/docs-for-tag?tag=Gamma&limit=3")
    assert resp.status_code == 200
    mock_docs.assert_called_once_with("proj-g", limit=3)
