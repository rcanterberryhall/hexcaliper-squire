import json
import time
from unittest.mock import patch

import pytest
from app import todos, analyses, scan_logs, scan_state, intel_tbl, _save_analysis, Q
from models import Analysis, ActionItem


def _mock_analysis(item_id="x1", source="outlook"):
    return Analysis(
        item_id=item_id, source=source, title="T", author="a",
        timestamp="2024-01-01T00:00:00", url="", has_action=False,
        priority="low", category="fyi", action_items=[], summary="S",
        urgency_reason=None,
    )


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
    with patch("orchestrator.analyze", return_value=_mock_analysis()):
        r = client.post("/ingest", json={"items": [_item("a"), _item("b")]})
    assert r.status_code == 200
    assert r.json()["received"] == 2
    assert r.json()["skipped"] == 0


def test_ingest_skips_items_without_item_id(client):
    with patch("orchestrator.analyze", return_value=_mock_analysis()):
        r = client.post("/ingest", json={"items": [{"source": "outlook", "title": "no id"}]})
    assert r.json()["received"] == 0
    assert r.json()["skipped"] == 1


def test_ingest_deduplicates_already_processed(client):
    """Items already in the analyses table should be skipped."""
    from app import analyses, Q
    analyses.insert({"item_id": "dup1", "source": "outlook"})

    with patch("orchestrator.analyze", return_value=_mock_analysis()):
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


def test_post_todo_creates_manual_item(client):
    r = client.post("/todos", json={"description": "Manual task", "priority": "high"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "doc_id" in body
    row = todos.get(doc_id=body["doc_id"])
    assert row is not None
    assert row["description"] == "Manual task"
    assert row["priority"]    == "high"
    assert row["is_manual"]   == 1


def test_post_todo_requires_description(client):
    r = client.post("/todos", json={"priority": "low"})
    assert r.status_code == 400


def test_post_todo_sets_deadline_and_project(client):
    r = client.post("/todos", json={
        "description": "Deadline task",
        "deadline":    "2026-12-31",
        "project_tag": "Alpha",
    })
    assert r.status_code == 200
    row = todos.get(doc_id=r.json()["doc_id"])
    assert row["deadline"]    == "2026-12-31"
    assert row["project_tag"] == "Alpha"


def test_patch_todo_edits_description_and_deadline(client):
    doc_id = _insert_todo(item_id="e1")
    r = client.patch(f"/todos/{doc_id}", json={"description": "Updated desc", "deadline": "2026-06-01"})
    assert r.status_code == 200
    row = todos.get(doc_id=doc_id)
    assert row["description"] == "Updated desc"
    assert row["deadline"]    == "2026-06-01"


def test_patch_todo_edits_priority(client):
    doc_id = _insert_todo(item_id="e2", priority="low")
    r = client.patch(f"/todos/{doc_id}", json={"priority": "high"})
    assert r.status_code == 200
    assert todos.get(doc_id=doc_id)["priority"] == "high"


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
    with patch("orchestrator.connector_slack.fetch", return_value=[]), \
         patch("orchestrator.connector_github.fetch", return_value=[]), \
         patch("orchestrator.connector_jira.fetch", return_value=[]), \
         patch("orchestrator.connector_outlook.fetch", return_value=[]), \
         patch("orchestrator.analyze", return_value=_mock_analysis()):
        r = client.post("/scan", json={"sources": ["github", "slack"]})

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "started"
    assert "github" in body["sources"]


# ── /settings ─────────────────────────────────────────────────────────────────

def test_get_settings_returns_expected_keys(client):
    r = client.get("/settings")
    assert r.status_code == 200
    body = r.json()
    for key in ["ollama_url", "ollama_model", "slack_client_id", "slack_client_secret",
                "github_pat", "jira_token", "jira_domain", "lookback_hours", "warnings"]:
        assert key in body


def test_get_settings_masks_secrets(client):
    import config as cfg
    cfg.SLACK_CLIENT_SECRET = "xoxb-realtoken123"
    cfg.GITHUB_PAT          = "ghp_realpat456"
    r = client.get("/settings")
    body = r.json()
    assert "•" in body["slack_client_secret"]
    assert "xoxb" in body["slack_client_secret"]   # prefix visible
    assert "realtoken123" not in body["slack_client_secret"]
    assert "•" in body["github_pat"]


def test_post_settings_saves_and_applies(client):
    r = client.post("/settings", json={
        "ollama_model":    "mistral:7b",
        "github_username": "testuser",
        "lookback_hours":  24,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True

    import config as cfg
    assert cfg.OLLAMA_MODEL   == "mistral:7b"
    assert cfg.GITHUB_USERNAME == "testuser"
    assert cfg.LOOKBACK_HOURS  == 24


def test_post_settings_ignores_masked_values(client):
    import config as cfg
    cfg.SLACK_BOT_TOKEN = "xoxb-original"
    # POST a masked value — server should leave original intact
    client.post("/settings", json={"slack_bot_token": "xoxb•••••••••"})
    assert cfg.SLACK_BOT_TOKEN == "xoxb-original"


def test_post_settings_persists_across_get(client):
    client.post("/settings", json={"github_username": "persisteduser"})
    r = client.get("/settings")
    assert r.json()["github_username"] == "persisteduser"


# ── /scan (continued) ─────────────────────────────────────────────────────────

def test_scan_rejects_concurrent_scan(client):
    scan_state["running"] = True
    try:
        r = client.post("/scan", json={"sources": ["github"]})
        assert r.status_code == 409
    finally:
        scan_state["running"] = False


# ── _save_analysis ─────────────────────────────────────────────────────────────

def _make_analysis(item_id="z1", has_action=False, category="fyi",
                   action_items=None, information_items=None):
    return Analysis(
        item_id=item_id, source="outlook", title="Test item",
        author="alice", timestamp="2026-03-17T10:00:00+00:00",
        url="", has_action=has_action, priority="medium",
        category=category,
        action_items=action_items or [],
        summary="S", urgency_reason=None,
        information_items=information_items or [],
    )


def test_save_analysis_preserves_situation_id():
    """situation_id set on an existing record must survive a re-save."""
    analyses.insert({
        "item_id": "z1", "source": "outlook", "title": "T",
        "situation_id": "sit-42",
        "priority": "medium", "category": "fyi",
    })
    _save_analysis(_make_analysis("z1"))
    stored = analyses.get(Q.item_id == "z1")
    assert stored["situation_id"] == "sit-42"


def test_save_analysis_does_not_duplicate_todos():
    """Calling _save_analysis twice with the same action item must produce
    exactly one todo row."""
    action = ActionItem(description="Do the thing", deadline=None, owner="me")
    a = _make_analysis("z2", has_action=True, category="task",
                       action_items=[action])
    _save_analysis(a)
    _save_analysis(a)
    assert len(todos.all()) == 1


def test_save_analysis_does_not_duplicate_intel():
    """Calling _save_analysis twice with the same information_item must produce
    exactly one intel row."""
    a = _make_analysis("z3", information_items=[
        {"fact": "Server was rebooted", "relevance": "ops context"}
    ])
    _save_analysis(a)
    _save_analysis(a)
    assert len(intel_tbl.all()) == 1


# ── /analyses PATCH ────────────────────────────────────────────────────────────

def test_patch_analysis_noise_removes_todos(client):
    _insert_analysis(item_id="n1")
    todos.insert({
        "item_id": "n1", "source": "github", "title": "T",
        "url": "", "description": "Do it", "deadline": None,
        "owner": "me", "priority": "medium", "done": False,
        "created_at": "2026-03-17T10:00:00+00:00",
    })
    r = client.patch("/analyses/n1", json={"category": "noise"})
    assert r.status_code == 200
    assert todos.get(Q.item_id == "n1") is None


def test_patch_analysis_priority_syncs_to_todos(client):
    _insert_analysis(item_id="p1", priority="low")
    todos.insert({
        "item_id": "p1", "source": "github", "title": "T",
        "url": "", "description": "Do it", "deadline": None,
        "owner": "me", "priority": "low", "done": False,
        "created_at": "2026-03-17T10:00:00+00:00",
    })
    r = client.patch("/analyses/p1", json={"priority": "high"})
    assert r.status_code == 200
    assert todos.get(Q.item_id == "p1")["priority"] == "high"


# ── /reanalyze ─────────────────────────────────────────────────────────────────

def test_reanalyze_rejects_concurrent_scan(client):
    scan_state["running"] = True
    try:
        r = client.post("/reanalyze")
        assert r.status_code == 409
    finally:
        scan_state["running"] = False


# ── /briefing ──────────────────────────────────────────────────────────────────

def test_get_briefing_empty(client):
    """GET /briefing returns empty dict when no briefing has been generated."""
    r = client.get("/briefing")
    assert r.status_code == 200
    assert r.json() == {}


def test_post_briefing_generate_returns_ok(client):
    """POST /briefing/generate schedules the job and returns ok immediately."""
    from unittest.mock import patch
    with patch("app._build_briefing", return_value={"sections": []}):
        r = client.post("/briefing/generate")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_get_briefing_returns_cached_content(client):
    """GET /briefing returns the most recently saved briefing."""
    import db
    content = {"sections": [{"project": "Alpha", "summary": "All good.", "situations": [], "todos": []}]}
    with db.lock:
        db.save_briefing(content)
    r = client.get("/briefing")
    assert r.status_code == 200
    body = r.json()
    assert "sections" in body
    assert len(body["sections"]) == 1
    assert body["sections"][0]["project"] == "Alpha"
    assert "generated_at" in body
