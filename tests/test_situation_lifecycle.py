"""
test_situation_lifecycle.py — B5: situation lifecycle workflow.

Tests for lifecycle_status enum, follow_up_date, notes, situation_events,
and the new transition + events endpoints.
"""
import uuid
import pytest
from tinydb import Query
from app import situations_tbl

Q = Query()


def _sit(sit_id=None, lifecycle_status="new", follow_up_date=None, notes="", dismissed=False):
    sit_id = sit_id or str(uuid.uuid4())
    return {
        "situation_id":     sit_id,
        "title":            "Test",
        "summary":          "Summary",
        "status":           "in_progress",
        "item_ids":         [],
        "sources":          ["jira"],
        "project_tag":      None,
        "score":            1.0,
        "priority":         "medium",
        "open_actions":     [],
        "references":       [],
        "key_context":      None,
        "last_updated":     "2026-04-05T00:00:00+00:00",
        "created_at":       "2026-04-05T00:00:00+00:00",
        "score_updated_at": "2026-04-05T00:00:00+00:00",
        "dismissed":        dismissed,
        "lifecycle_status": lifecycle_status,
        "follow_up_date":   follow_up_date,
        "notes":            notes,
    }


# ── GET /situations lifecycle filter ─────────────────────────────────────────

def test_default_filter_excludes_resolved_and_dismissed(client):
    """Default view returns only new/investigating/waiting."""
    situations_tbl.insert(_sit(sit_id="a", lifecycle_status="new"))
    situations_tbl.insert(_sit(sit_id="b", lifecycle_status="investigating"))
    situations_tbl.insert(_sit(sit_id="c", lifecycle_status="waiting"))
    situations_tbl.insert(_sit(sit_id="d", lifecycle_status="resolved"))
    situations_tbl.insert(_sit(sit_id="e", lifecycle_status="dismissed", dismissed=True))
    r = client.get("/situations")
    ids = {s["situation_id"] for s in r.json()}
    assert ids == {"a", "b", "c"}


def test_include_resolved(client):
    situations_tbl.insert(_sit(sit_id="r1", lifecycle_status="resolved"))
    situations_tbl.insert(_sit(sit_id="r2", lifecycle_status="new"))
    r = client.get("/situations?include_resolved=true")
    ids = {s["situation_id"] for s in r.json()}
    assert "r1" in ids
    assert "r2" in ids


def test_include_dismissed(client):
    situations_tbl.insert(_sit(sit_id="d1", lifecycle_status="dismissed", dismissed=True))
    r = client.get("/situations?include_dismissed=true")
    ids = {s["situation_id"] for s in r.json()}
    assert "d1" in ids


def test_lifecycle_status_filter(client):
    situations_tbl.insert(_sit(sit_id="w1", lifecycle_status="waiting"))
    situations_tbl.insert(_sit(sit_id="n1", lifecycle_status="new"))
    r = client.get("/situations?lifecycle_status=waiting")
    ids = {s["situation_id"] for s in r.json()}
    assert ids == {"w1"}


# ── Response includes new fields ─────────────────────────────────────────────

def test_response_includes_lifecycle_fields(client):
    situations_tbl.insert(_sit(sit_id="lf1", lifecycle_status="investigating",
                                follow_up_date="2099-01-01", notes="keep an eye on this"))
    r = client.get("/situations/lf1")
    body = r.json()
    assert body["lifecycle_status"] == "investigating"
    assert body["follow_up_date"] == "2099-01-01"
    assert body["notes"] == "keep an eye on this"
    assert "follow_up_overdue" in body


# ── PATCH /situations supports new fields ────────────────────────────────────

def test_patch_lifecycle_status(client):
    situations_tbl.insert(_sit(sit_id="pl1"))
    r = client.patch("/situations/pl1", json={"lifecycle_status": "investigating"})
    assert r.status_code == 200
    sit = situations_tbl.get(Q.situation_id == "pl1")
    assert sit["lifecycle_status"] == "investigating"


def test_patch_lifecycle_status_invalid(client):
    situations_tbl.insert(_sit(sit_id="pl2"))
    r = client.patch("/situations/pl2", json={"lifecycle_status": "bogus"})
    assert r.status_code == 422


def test_patch_notes(client):
    situations_tbl.insert(_sit(sit_id="pn1"))
    r = client.patch("/situations/pn1", json={"notes": "Follow up next week"})
    assert r.status_code == 200
    sit = situations_tbl.get(Q.situation_id == "pn1")
    assert sit["notes"] == "Follow up next week"


def test_patch_follow_up_date(client):
    situations_tbl.insert(_sit(sit_id="pf1"))
    r = client.patch("/situations/pf1", json={"follow_up_date": "2026-05-01"})
    assert r.status_code == 200
    sit = situations_tbl.get(Q.situation_id == "pf1")
    assert sit["follow_up_date"] == "2026-05-01"


# ── POST /situations/{id}/transition ─────────────────────────────────────────

def test_transition_changes_lifecycle_status(client):
    situations_tbl.insert(_sit(sit_id="tr1"))
    r = client.post("/situations/tr1/transition", json={"to_status": "investigating"})
    assert r.status_code == 200
    assert r.json()["lifecycle_status"] == "investigating"
    sit = situations_tbl.get(Q.situation_id == "tr1")
    assert sit["lifecycle_status"] == "investigating"


def test_transition_to_dismissed_sets_dismissed_flag(client):
    situations_tbl.insert(_sit(sit_id="tr2"))
    r = client.post("/situations/tr2/transition", json={"to_status": "dismissed"})
    assert r.status_code == 200
    sit = situations_tbl.get(Q.situation_id == "tr2")
    assert sit["dismissed"] is True
    assert sit["lifecycle_status"] == "dismissed"


def test_transition_invalid_status(client):
    situations_tbl.insert(_sit(sit_id="tr3"))
    r = client.post("/situations/tr3/transition", json={"to_status": "banana"})
    assert r.status_code == 422


def test_transition_404(client):
    r = client.post("/situations/nope/transition", json={"to_status": "investigating"})
    assert r.status_code == 404


# ── GET /situations/{id}/events ───────────────────────────────────────────────

def test_events_recorded_on_transition(client):
    situations_tbl.insert(_sit(sit_id="ev1"))
    client.post("/situations/ev1/transition", json={"to_status": "investigating", "note": "starting"})
    client.post("/situations/ev1/transition", json={"to_status": "waiting"})
    r = client.get("/situations/ev1/events")
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 2
    assert events[0]["to_status"] == "investigating"
    assert events[0]["note"] == "starting"
    assert events[1]["to_status"] == "waiting"


def test_events_empty_for_new_situation(client):
    situations_tbl.insert(_sit(sit_id="ev2"))
    r = client.get("/situations/ev2/events")
    assert r.status_code == 200
    assert r.json() == []


def test_events_404(client):
    r = client.get("/situations/nope/events")
    assert r.status_code == 404
