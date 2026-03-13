import json
import time
from unittest.mock import patch

import pytest
from app import todos, analyses, scan_logs, scan_state


# ── /health ───────────────────────────────────────────────────────────────────

def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "warnings" in body
    assert isinstance(body["warnings"], list)


# ── /ingest ───────────────────────────────────────────────────────────────────

def _item(item_id="x1", source="outlook"):
    return {
        "item_id": item_id,
        "source": source,
        "title": "Test email",
        "body": "Please review.",
        "url": "http://example.com",
        "author": "alice@example.com",
        "timestamp": "2024-01-01T00:00:00",
    }


def test_ingest_returns_received_count(client):
    with patch("app.analyze_batch", return_value=[]):
        r = client.post("/ingest", json={"items": [_item("a"), _item("b")]})
    assert r.status_code == 200
    assert r.json()["received"] == 2
    assert r.json()["skipped"] == 0


def test_ingest_skips_items_without_item_id(client):
    with patch("app.analyze_batch", return_value=[]):
        r = client.post("/ingest", json={"items": [{"source": "outlook", "title": "no id"}]})
    assert r.json()["received"] == 0
    assert r.json()["skipped"] == 1


def test_ingest_deduplicates_already_processed(client):
    """Items already in the analyses table should be skipped."""
    from app import analyses, Q
    analyses.insert({"item_id": "dup1", "source": "outlook"})

    with patch("app.analyze_batch", return_value=[]):
        r = client.post("/ingest", json={"items": [_item("dup1"), _item("new1")]})

    assert r.json()["skipped"] == 1
    assert r.json()["received"] == 1


# ── /todos ────────────────────────────────────────────────────────────────────

def _insert_todo(**kwargs):
    defaults = {
        "item_id": "x", "source": "github", "title": "Fix bug",
        "url": "", "description": "Do it", "deadline": None,
        "owner": "me", "priority": "medium", "done": False,
        "created_at": "2024-01-01T00:00:00",
    }
    defaults.update(kwargs)
    return todos.insert(defaults)


def test_get_todos_empty(client):
    r = client.get("/todos")
    assert r.status_code == 200
    assert r.json() == []


def test_get_todos_returns_open_by_default(client):
    _insert_todo(item_id="t1", done=False)
    _insert_todo(item_id="t2", done=True)
    r = client.get("/todos")
    data = r.json()
    assert len(data) == 1
    assert data[0]["item_id"] == "t1"


def test_get_todos_include_done(client):
    _insert_todo(item_id="t1", done=False)
    _insert_todo(item_id="t2", done=True)
    r = client.get("/todos?done=true")
    assert len(r.json()) == 2


def test_get_todos_filter_by_source(client):
    _insert_todo(item_id="s1", source="slack")
    _insert_todo(item_id="g1", source="github")
    r = client.get("/todos?source=slack")
    data = r.json()
    assert all(t["source"] == "slack" for t in data)
    assert len(data) == 1


def test_get_todos_filter_by_priority(client):
    _insert_todo(item_id="h1", priority="high")
    _insert_todo(item_id="l1", priority="low")
    r = client.get("/todos?priority=high")
    data = r.json()
    assert len(data) == 1
    assert data[0]["priority"] == "high"


def test_get_todos_sorted_by_priority(client):
    _insert_todo(item_id="l1", priority="low")
    _insert_todo(item_id="h1", priority="high")
    _insert_todo(item_id="m1", priority="medium")
    r = client.get("/todos")
    priorities = [t["priority"] for t in r.json()]
    assert priorities == ["high", "medium", "low"]


def test_patch_todo_marks_done(client):
    doc_id = _insert_todo(item_id="p1", done=False)
    r = client.patch(f"/todos/{doc_id}", json={"done": True})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert todos.get(doc_id=doc_id)["done"] is True


def test_patch_todo_ignores_unknown_fields(client):
    doc_id = _insert_todo(item_id="p2")
    r = client.patch(f"/todos/{doc_id}", json={"unknown_field": "value"})
    assert r.status_code == 200


def test_delete_todo(client):
    doc_id = _insert_todo(item_id="d1")
    r = client.delete(f"/todos/{doc_id}")
    assert r.status_code == 204
    assert todos.get(doc_id=doc_id) is None


# ── /analyses ─────────────────────────────────────────────────────────────────

def _insert_analysis(**kwargs):
    defaults = {
        "item_id": "a1", "source": "github", "title": "T",
        "author": "alice", "timestamp": "2024-01-01T00:00:00",
        "url": "", "has_action": True, "priority": "medium",
        "category": "task", "summary": "S", "urgency": None,
        "action_items": "[]", "processed_at": "2024-01-01T00:00:00",
    }
    defaults.update(kwargs)
    return analyses.insert(defaults)


def test_get_analyses_empty(client):
    r = client.get("/analyses")
    assert r.status_code == 200
    assert r.json() == []


def test_get_analyses_filter_by_source(client):
    _insert_analysis(item_id="g1", source="github")
    _insert_analysis(item_id="s1", source="slack")
    r = client.get("/analyses?source=github")
    data = r.json()
    assert len(data) == 1
    assert data[0]["source"] == "github"


def test_get_analyses_filter_by_category(client):
    _insert_analysis(item_id="t1", category="task")
    _insert_analysis(item_id="r1", category="review")
    r = client.get("/analyses?category=review")
    data = r.json()
    assert len(data) == 1
    assert data[0]["category"] == "review"


# ── /stats ────────────────────────────────────────────────────────────────────

def test_stats_empty_db(client):
    r = client.get("/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_items"] == 0
    assert body["open_todos"] == 0
    assert body["high_priority"] == 0
    assert body["by_source"] == []
    assert body["by_category"] == []
    assert body["last_scan"] is None


def test_stats_counts_correctly(client):
    _insert_analysis(item_id="a1", category="task")
    _insert_analysis(item_id="a2", category="task")
    _insert_todo(item_id="a1", source="github", priority="high", done=False)
    _insert_todo(item_id="a2", source="slack", priority="low", done=False)

    r = client.get("/stats")
    body = r.json()
    assert body["total_items"] == 2
    assert body["open_todos"] == 2
    assert body["high_priority"] == 1
    sources = {s["source"]: s["count"] for s in body["by_source"]}
    assert sources["github"] == 1
    assert sources["slack"] == 1


# ── /scan ─────────────────────────────────────────────────────────────────────

def test_scan_status_returns_state(client):
    r = client.get("/scan/status")
    assert r.status_code == 200
    body = r.json()
    assert "running" in body
    assert "message" in body


def test_scan_starts_and_returns_sources(client):
    # Patch connectors and agent so no real I/O happens
    with patch("app.connector_slack.fetch", return_value=[]), \
         patch("app.connector_github.fetch", return_value=[]), \
         patch("app.connector_jira.fetch", return_value=[]), \
         patch("app.connector_outlook.fetch", return_value=[]), \
         patch("app.analyze_batch", return_value=[]):
        r = client.post("/scan", json={"sources": ["github", "slack"]})

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "started"
    assert "github" in body["sources"]


def test_scan_rejects_concurrent_scan(client):
    scan_state["running"] = True
    try:
        r = client.post("/scan", json={"sources": ["github"]})
        assert r.status_code == 409
    finally:
        scan_state["running"] = False
