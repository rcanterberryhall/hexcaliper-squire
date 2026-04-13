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


# ── POST /situations/{id}/split ──────────────────────────────────────────────


def test_split_situation_happy_path(client):
    analyses.insert(_analysis("sp-a"))
    analyses.insert(_analysis("sp-b", source="slack"))
    analyses.insert(_analysis("sp-c", source="teams"))
    situations_tbl.insert(_sit(sit_id="sp-1", item_ids=["sp-a", "sp-b", "sp-c"]))
    r = client.post("/situations/sp-1/split",
                    json={"item_ids": ["sp-c"], "new_title": "Broken out"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["original_situation_id"] == "sp-1"
    new_id = body["new_situation_id"]
    src = situations_tbl.get(Q.situation_id == "sp-1")
    new = situations_tbl.get(Q.situation_id == new_id)
    assert src["item_ids"] == ["sp-a", "sp-b"]
    assert new["item_ids"] == ["sp-c"]
    assert new["title"] == "Broken out"


def test_split_situation_rejects_emptying_source(client):
    analyses.insert(_analysis("sp2-a"))
    analyses.insert(_analysis("sp2-b"))
    situations_tbl.insert(_sit(sit_id="sp-2", item_ids=["sp2-a", "sp2-b"]))
    r = client.post("/situations/sp-2/split",
                    json={"item_ids": ["sp2-a", "sp2-b"]})
    assert r.status_code == 400


def test_split_situation_404(client):
    r = client.post("/situations/nope/split", json={"item_ids": ["x"]})
    assert r.status_code == 404


def test_split_situation_rejects_empty_list(client):
    situations_tbl.insert(_sit(sit_id="sp-3"))
    r = client.post("/situations/sp-3/split", json={"item_ids": []})
    assert r.status_code == 400


# ── POST /situations/{id}/merge ──────────────────────────────────────────────


def test_merge_situations_happy_path(client):
    analyses.insert(_analysis("mg-a"))
    analyses.insert(_analysis("mg-b", source="slack"))
    analyses.insert(_analysis("mg-c", source="teams"))
    situations_tbl.insert(_sit(sit_id="mg-tgt", item_ids=["mg-a"]))
    situations_tbl.insert(_sit(sit_id="mg-src", item_ids=["mg-b", "mg-c"]))
    r = client.post("/situations/mg-tgt/merge",
                    json={"source_situation_id": "mg-src"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["situation_id"] == "mg-tgt"
    tgt = situations_tbl.get(Q.situation_id == "mg-tgt")
    src = situations_tbl.get(Q.situation_id == "mg-src")
    assert set(tgt["item_ids"]) == {"mg-a", "mg-b", "mg-c"}
    assert src["dismissed"] is True
    assert src["dismiss_reason"] == "merged_into:mg-tgt"
    assert src["item_ids"] == []


def test_merge_situations_404(client):
    situations_tbl.insert(_sit(sit_id="mg-only"))
    r = client.post("/situations/mg-only/merge",
                    json={"source_situation_id": "doesntexist"})
    assert r.status_code == 404


def test_merge_situations_self_merge_rejected(client):
    situations_tbl.insert(_sit(sit_id="mg-self"))
    r = client.post("/situations/mg-self/merge",
                    json={"source_situation_id": "mg-self"})
    assert r.status_code == 400


def test_merge_requires_source_situation_id(client):
    situations_tbl.insert(_sit(sit_id="mg-x"))
    r = client.post("/situations/mg-x/merge", json={})
    assert r.status_code == 400


# ── stale_flag in /situations response ───────────────────────────────────────


def test_stale_flag_none_for_fresh_situation(client):
    analyses.insert(_analysis("st-a"))
    analyses.insert(_analysis("st-b", source="slack"))
    sit = _sit(sit_id="st-fresh", item_ids=["st-a", "st-b"])
    sit["lifecycle_status"] = "waiting"
    # last_updated is just now (set by _sit fixture to a recent date)
    from datetime import datetime, timezone
    sit["last_updated"] = datetime.now(timezone.utc).isoformat()
    situations_tbl.insert(sit)
    r = client.get("/situations/st-fresh")
    assert r.status_code == 200
    assert r.json()["stale_flag"] is None


def test_stale_flag_waiting_after_threshold(client):
    from datetime import datetime, timezone, timedelta
    analyses.insert(_analysis("st2-a"))
    analyses.insert(_analysis("st2-b", source="slack"))
    sit = _sit(sit_id="st-old", item_ids=["st2-a", "st2-b"])
    sit["lifecycle_status"] = "waiting"
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    sit["last_updated"] = old
    situations_tbl.insert(sit)
    r = client.get("/situations/st-old")
    assert r.status_code == 200
    assert r.json()["stale_flag"] == "stale_waiting"


def test_stale_flag_investigating_requires_longer_threshold(client):
    from datetime import datetime, timezone, timedelta
    analyses.insert(_analysis("st3-a"))
    analyses.insert(_analysis("st3-b", source="slack"))
    # 10 days old: past waiting threshold (7) but not investigating (14)
    sit = _sit(sit_id="st-inv-fresh", item_ids=["st3-a", "st3-b"])
    sit["lifecycle_status"] = "investigating"
    sit["last_updated"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    situations_tbl.insert(sit)
    r = client.get("/situations/st-inv-fresh")
    assert r.json()["stale_flag"] is None

    # 20 days: past investigating threshold
    sit2 = _sit(sit_id="st-inv-old", item_ids=["st3-a", "st3-b"])
    sit2["lifecycle_status"] = "investigating"
    sit2["last_updated"] = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    situations_tbl.insert(sit2)
    r2 = client.get("/situations/st-inv-old")
    assert r2.json()["stale_flag"] == "stale_investigating"


def test_stale_flag_ignored_for_new_status(client):
    from datetime import datetime, timezone, timedelta
    analyses.insert(_analysis("st4-a"))
    analyses.insert(_analysis("st4-b", source="slack"))
    sit = _sit(sit_id="st-new-old", item_ids=["st4-a", "st4-b"])
    # lifecycle_status default in _sit is absent; ensure explicit
    sit["lifecycle_status"] = "new"
    sit["last_updated"] = (datetime.now(timezone.utc) - timedelta(days=99)).isoformat()
    situations_tbl.insert(sit)
    r = client.get("/situations/st-new-old")
    assert r.json()["stale_flag"] is None
