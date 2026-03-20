"""tests/test_situations.py — Tests for the situation layer endpoints."""
import uuid

import pytest
from tinydb import Query

from app import situations_tbl, analyses, intel_tbl

Q = Query()


def _sit(sit_id=None, score=1.5, dismissed=False, item_ids=None, sources=None):
    sit_id = sit_id or str(uuid.uuid4())
    return {
        "situation_id":     sit_id,
        "title":            "Test Situation",
        "summary":          "Things are happening.",
        "status":           "in_progress",
        "item_ids":         item_ids or ["a1", "a2"],
        "sources":          sources or ["jira", "slack"],
        "project_tag":      None,
        "score":            score,
        "priority":         "medium",
        "open_actions":     [],
        "references":       [],
        "key_context":      None,
        "last_updated":     "2026-03-17T10:00:00+00:00",
        "created_at":       "2026-03-17T10:00:00+00:00",
        "score_updated_at": "2026-03-17T10:00:00+00:00",
        "dismissed":        dismissed,
    }


def _analysis(item_id, source="jira"):
    return {
        "item_id": item_id, "source": source, "title": f"Item {item_id}",
        "author": "a", "timestamp": "2026-03-17T10:00:00+00:00", "url": "",
        "has_action": False, "priority": "medium", "category": "fyi",
        "summary": "S", "urgency": None, "action_items": "[]",
        "processed_at": "2026-03-17T10:00:00+00:00",
    }


def test_get_situations_empty(client):
    r = client.get("/situations")
    assert r.status_code == 200
    assert r.json() == []


def test_get_situations_returns_active(client):
    situations_tbl.insert(_sit(sit_id="s1", score=1.5))
    situations_tbl.insert(_sit(sit_id="s2", score=0.8))
    situations_tbl.insert(_sit(sit_id="s3", dismissed=True))
    r = client.get("/situations")
    data = r.json()
    assert len(data) == 2
    assert data[0]["situation_id"] == "s1"
    assert data[1]["situation_id"] == "s2"


def test_get_situations_excludes_dismissed(client):
    situations_tbl.insert(_sit(dismissed=True))
    r = client.get("/situations")
    assert r.json() == []


def test_get_situation_detail(client):
    analyses.insert(_analysis("a1"))
    analyses.insert(_analysis("a2", source="slack"))
    situations_tbl.insert(_sit(sit_id="detail-1"))
    r = client.get("/situations/detail-1")
    assert r.status_code == 200
    body = r.json()
    assert body["situation_id"] == "detail-1"
    assert len(body["items"]) == 2


def test_get_situation_404(client):
    r = client.get("/situations/nonexistent")
    assert r.status_code == 404


def test_dismiss_situation(client):
    situations_tbl.insert(_sit(sit_id="dis-1"))
    r = client.post("/situations/dis-1/dismiss")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    sit = situations_tbl.get(Q.situation_id == "dis-1")
    assert sit["dismissed"] is True


def test_dismiss_situation_404(client):
    r = client.post("/situations/nope/dismiss")
    assert r.status_code == 404


def test_patch_situation_title(client):
    situations_tbl.insert(_sit(sit_id="patch-1"))
    r = client.patch("/situations/patch-1", json={"title": "Updated Title"})
    assert r.status_code == 200
    sit = situations_tbl.get(Q.situation_id == "patch-1")
    assert sit["title"] == "Updated Title"


def test_patch_situation_rejects_unknown_fields(client):
    situations_tbl.insert(_sit(sit_id="patch-2"))
    r = client.patch("/situations/patch-2", json={"score": 99.0})
    assert r.status_code == 400


def test_stats_includes_situation_counts(client):
    situations_tbl.insert(_sit(sit_id="s1", score=1.6))
    situations_tbl.insert(_sit(sit_id="s2", score=0.5))
    situations_tbl.insert(_sit(sit_id="s3", dismissed=True))
    r = client.get("/stats")
    body = r.json()
    assert body["open_situations"] == 2
    assert body["high_score_situations"] == 1
