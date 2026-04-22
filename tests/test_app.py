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


def test_ingest_deduplicates_in_flight_items(client):
    """parsival#58: a second /ingest call issued before the first's background
    task finishes must not re-queue items that are still being processed."""
    import orchestrator
    # Simulate a slow / not-yet-run background task by patching
    # process_ingest_items to be a no-op. claim_ingest_items still runs in
    # the request handler, so the first call's ids are in _in_flight_ids
    # when the second call arrives.
    with patch("orchestrator.process_ingest_items"):
        r1 = client.post("/ingest", json={"items": [_item("a"), _item("b")]})
        r2 = client.post("/ingest", json={"items": [_item("a"), _item("c")]})

    assert r1.json() == {"received": 2, "skipped": 0}
    assert r2.json() == {"received": 1, "skipped": 1}
    # Manually release so we don't leak state across tests (conftest also clears).
    orchestrator._in_flight_ids.clear()


def test_ingest_deduplicates_within_single_batch(client):
    """A batch containing the same item_id twice must only queue it once."""
    with patch("orchestrator.process_ingest_items"):
        r = client.post("/ingest", json={"items": [_item("x"), _item("x")]})

    assert r.json() == {"received": 1, "skipped": 1}


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


def test_get_todos_assigned_count_empty(client):
    r = client.get("/todos/assigned_count")
    assert r.status_code == 200
    assert r.json() == {"count": 0}


def test_get_todos_assigned_count_counts_only_open_assigned(client):
    # Matches: status=assigned, assigned_to set, done=False
    _insert_todo(item_id="a1", status="assigned", assigned_to="bob@example.com", done=False)
    _insert_todo(item_id="a2", status="assigned", assigned_to="alice@example.com", done=False)
    # Excluded: done even though still labeled assigned
    _insert_todo(item_id="a3", status="assigned", assigned_to="bob@example.com", done=True)
    # Excluded: open (unassigned) item
    _insert_todo(item_id="o1", status="open", done=False)
    # Excluded: status=assigned but assigned_to empty / missing
    _insert_todo(item_id="a4", status="assigned", assigned_to="", done=False)
    _insert_todo(item_id="a5", status="assigned", assigned_to=None, done=False)

    r = client.get("/todos/assigned_count")
    assert r.status_code == 200
    assert r.json() == {"count": 2}


def test_get_todos_assigned_count_ignores_priority_and_source_filters(client):
    """The badge count is intentionally unfiltered — it shouldn't honor query params."""
    _insert_todo(item_id="a1", status="assigned", assigned_to="bob@example.com",
                 priority="high", source="slack")
    _insert_todo(item_id="a2", status="assigned", assigned_to="alice@example.com",
                 priority="low", source="github")
    r = client.get("/todos/assigned_count?priority=high&source=slack")
    assert r.status_code == 200
    # Both rows counted despite the filters — the endpoint accepts no params.
    assert r.json() == {"count": 2}


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


def test_get_analysis_by_item_id_returns_single_record(client):
    _insert_analysis(item_id="solo1", category="task", title="Just me")
    _insert_analysis(item_id="other", category="task", title="Different")
    r = client.get("/analyses/solo1")
    assert r.status_code == 200
    body = r.json()
    assert body["item_id"] == "solo1"
    assert body["title"] == "Just me"
    assert "attention_score" in body


def test_get_analysis_by_item_id_missing_returns_404(client):
    r = client.get("/analyses/does-not-exist")
    assert r.status_code == 404


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


def test_save_analysis_dedups_todos_across_conversation_thread():
    """parsival#77: two replies in the same Outlook thread (different
    item_ids, same conversation_id) with near-identical action items must
    only produce one todo row."""
    action1 = ActionItem(description="Receive replacement transformer", deadline=None, owner="me")
    action2 = ActionItem(description="receive replacement transformer.", deadline=None, owner="me")
    a1 = _make_analysis("reply-1", has_action=True, category="task", action_items=[action1])
    a2 = _make_analysis("reply-2", has_action=True, category="task", action_items=[action2])
    a1.conversation_id = "conv-xfr"
    a2.conversation_id = "conv-xfr"
    _save_analysis(a1)
    _save_analysis(a2)
    assert len(todos.all()) == 1


def test_save_analysis_allows_same_description_in_different_conversations():
    """Don't dedup across unrelated threads — same wording, different
    conversation_id means two distinct tasks."""
    action = ActionItem(description="Review the quote", deadline=None, owner="me")
    a1 = _make_analysis("conv-a-1", has_action=True, category="task", action_items=[action])
    a2 = _make_analysis("conv-b-1", has_action=True, category="task", action_items=[action])
    a1.conversation_id = "thread-a"
    a2.conversation_id = "thread-b"
    _save_analysis(a1)
    _save_analysis(a2)
    assert len(todos.all()) == 2


# ── get_open_todos_for_conversation (parsival#79) ─────────────────────────────

def _insert_item_row(item_id: str, conversation_id: str, timestamp: str):
    import db as _db
    _db.conn().execute(
        "INSERT INTO items (item_id, conversation_id, timestamp) VALUES (?, ?, ?)",
        (item_id, conversation_id, timestamp),
    )


def _insert_todo_for(item_id: str, description: str, *, done: bool = False,
                     owner: str = "me", deadline: str | None = None):
    todos.insert({
        "item_id": item_id, "source": "outlook", "title": "T", "url": "",
        "description": description, "deadline": deadline, "owner": owner,
        "priority": "medium", "done": done,
        "created_at": "2026-03-17T10:00:00+00:00",
    })


def test_get_open_todos_for_conversation_returns_only_matching_thread():
    import db as _db
    _insert_item_row("a1", "conv-A", "2026-04-01T10:00:00+00:00")
    _insert_item_row("b1", "conv-B", "2026-04-01T10:00:00+00:00")
    _insert_todo_for("a1", "Ship the part")
    _insert_todo_for("b1", "Review quote")

    out = _db.get_open_todos_for_conversation("conv-A")

    descs = [t["description"] for t in out]
    assert descs == ["Ship the part"]


def test_get_open_todos_for_conversation_excludes_done_todos():
    import db as _db
    _insert_item_row("x1", "conv-X", "2026-04-01T10:00:00+00:00")
    _insert_todo_for("x1", "Open task", done=False)
    _insert_todo_for("x1", "Finished task", done=True)

    out = _db.get_open_todos_for_conversation("conv-X")

    descs = [t["description"] for t in out]
    assert descs == ["Open task"]


def test_get_open_todos_for_conversation_respects_before_timestamp():
    """Only todos from items with a strictly earlier timestamp are returned —
    prevents a message from self-suppressing its own todos on reanalyze."""
    import db as _db
    _insert_item_row("m1", "conv-T", "2026-04-01T09:00:00+00:00")
    _insert_item_row("m2", "conv-T", "2026-04-01T10:00:00+00:00")
    _insert_item_row("m3", "conv-T", "2026-04-01T11:00:00+00:00")
    _insert_todo_for("m1", "Task from message 1")
    _insert_todo_for("m2", "Task from message 2")
    _insert_todo_for("m3", "Task from message 3")

    out = _db.get_open_todos_for_conversation(
        "conv-T", before_timestamp="2026-04-01T10:30:00+00:00"
    )

    descs = sorted(t["description"] for t in out)
    assert descs == ["Task from message 1", "Task from message 2"]


def test_get_open_todos_for_conversation_empty_id_returns_empty():
    import db as _db
    assert _db.get_open_todos_for_conversation("") == []
    assert _db.get_open_todos_for_conversation(None) == []


def test_get_open_todos_for_conversation_caps_at_default_limit():
    """Long threads (50+ messages × one todo each) must not blow the prompt —
    helper caps at 15 most recent by item timestamp."""
    import db as _db
    for i in range(25):
        ts = f"2026-04-01T{i:02d}:00:00+00:00"
        _insert_item_row(f"i{i}", "conv-long", ts)
        _insert_todo_for(f"i{i}", f"Task {i}")

    out = _db.get_open_todos_for_conversation("conv-long")

    assert len(out) == 15
    descs = {t["description"] for t in out}
    # Most recent 15 messages are i10..i24 — i0..i9 should drop off.
    assert "Task 24" in descs
    assert "Task 10" in descs
    assert "Task 9" not in descs


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


def test_patch_analysis_priority_reason_records_override(client):
    """A priority change with a valid reason should append to PRIORITY_OVERRIDES."""
    import config
    import db as _db
    config.PRIORITY_OVERRIDES = []
    _insert_analysis(item_id="po1", priority="low", author="boss@example.com",
                     title="Weekly sync")
    r = client.patch("/analyses/po1", json={"priority": "high",
                                             "priority_reason": "person_matters"})
    assert r.status_code == 200
    # Background task runs synchronously under TestClient once the response returns.
    saved = _db.get_settings()
    overrides = saved.get("priority_overrides", [])
    assert len(overrides) == 1
    o = overrides[0]
    assert o["reason"]        == "person_matters"
    assert o["llm_priority"]  == "low"
    assert o["user_priority"] == "high"
    assert o["author"]        == "boss@example.com"
    assert o["title"]         == "Weekly sync"
    # config.PRIORITY_OVERRIDES is refreshed via apply_overrides.
    assert config.PRIORITY_OVERRIDES and \
        config.PRIORITY_OVERRIDES[-1]["reason"] == "person_matters"


def test_patch_analysis_invalid_priority_reason_ignored(client):
    """An unknown priority_reason must not pollute PRIORITY_OVERRIDES."""
    import config
    import db as _db
    config.PRIORITY_OVERRIDES = []
    _insert_analysis(item_id="po2", priority="medium")
    r = client.patch("/analyses/po2", json={"priority": "high",
                                             "priority_reason": "banana"})
    assert r.status_code == 200
    saved = _db.get_settings()
    assert not saved.get("priority_overrides")


def test_patch_analysis_priority_reason_without_priority_change_ignored(client):
    """If priority didn't actually change, no override is recorded."""
    import db as _db
    _insert_analysis(item_id="po3", priority="high")
    r = client.patch("/analyses/po3", json={"priority": "high",
                                             "priority_reason": "deadline_real"})
    assert r.status_code == 200
    saved = _db.get_settings()
    assert not saved.get("priority_overrides")


# ── /passdown/generate ────────────────────────────────────────────────────────

def test_passdown_generate_empty_db(client):
    """With no data, passdown still returns a valid structure."""
    r = client.post("/passdown/generate")
    assert r.status_code == 200
    body = r.json()
    assert "html" in body and "sections" in body
    assert body["hours"] == 12
    assert len(body["sections"]) == 5  # five canonical sections
    # Every section should have items=[] with no data inserted.
    for sec in body["sections"]:
        assert sec["items"] == []
    # HTML should still include the heading and an empty-state note.
    assert "Shift passdown" in body["html"]


def test_passdown_generate_populates_open_actions(client):
    """Open todos appear in the Open Action Items section."""
    _insert_analysis(item_id="pd1", priority="high", category="task")
    todos.insert({
        "item_id": "pd1", "source": "github", "title": "T",
        "url": "https://example.com/x", "description": "Ship the release",
        "deadline": "2026-04-15", "owner": "me", "priority": "high",
        "done": False, "created_at": "2026-04-12T08:00:00+00:00",
    })
    r = client.post("/passdown/generate", json={"hours": 24})
    body = r.json()
    actions = next(s for s in body["sections"] if s["kind"] == "actions")
    assert any(i["description"] == "Ship the release" for i in actions["items"])
    deadlines = next(s for s in body["sections"] if s["kind"] == "deadlines")
    assert any(i["deadline"] == "2026-04-15" for i in deadlines["items"])
    assert "Ship the release" in body["html"]


def test_passdown_generate_clamps_hours(client):
    """Non-integer or out-of-range hours must be clamped, not crash."""
    r = client.post("/passdown/generate", json={"hours": 9999})
    assert r.status_code == 200
    assert r.json()["hours"] == 168
    r = client.post("/passdown/generate", json={"hours": 0})
    assert r.json()["hours"] == 1


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


def test_save_analysis_assigns_delegated_owner_to_assigned_tab():
    """Regression guard for issue #83's Assigned-tab symptom: when the LLM
    produces a non-me owner, _save_analysis must set status='assigned' and
    populate assigned_to so the todo shows up in the Assigned tab."""
    import db as _db

    a = Analysis(
        item_id="test-rv17-83",
        source="outlook",
        title="RV17 and next up",
        author="Chris Ward <Chris.Ward@UniversalOrlando.com>",
        timestamp="2026-04-16T12:36:00",
        url="",
        category="task",
        task_type=None,
        has_action=True,
        priority="medium",
        action_items=[ActionItem(
            description="Coordinate with Reid Hall's team to determine next RV",
            deadline=None,
            owner="Anna Simonitis",
        )],
        summary="Coordinate next RV after RV17",
        urgency_reason=None,
        hierarchy="project",
        is_passdown=False,
        project_tag=None,
        direction="received",
        conversation_id="conv-83",
        conversation_topic=None,
        goals=[],
        key_dates=[],
        body_preview="Hi Tech team, RV17 is almost complete...",
        to_field="Anna Simonitis <Anna.Simonitis@universalorlando.com>; Reid Hall <reid.hall@prismsystems.com>",
        cc_field="",
        is_replied=False,
        replied_at=None,
        information_items=[],
    )

    _save_analysis(a)

    rows = _db.get_todos_for_item("test-rv17-83")
    assert len(rows) == 1, rows
    t = rows[0]
    assert t["owner"] == "Anna Simonitis"
    assert t["status"] == "assigned", t
    assert t["assigned_to"] == "anna.simonitis@universalorlando.com"


class TestManualTodoMigration:
    """Migration backfills items rows for pre-existing manual todos
    (is_manual=1, item_id IS NULL). The backfill sets item_id='manual_<todo_id>'
    on both the todo and the synthesized items row so the UI's
    openTodoDetail() → GET /analyses/{item_id} path works for them."""

    def test_backfill_creates_items_row_for_orphan_manual_todo(self, client):
        import db as _db
        # Insert a legacy manual todo bypassing the new POST handler so we
        # simulate the pre-migration schema state.
        with _db.lock:
            tid = _db.insert_todo({
                "description": "legacy manual todo",
                "priority":    "medium",
                "is_manual":   1,
                "done":        0,
                "status":      "open",
                "source":      "manual",
                "title":       "",
                "url":         "",
                "owner":       "me",
                "created_at":  "2026-04-20T00:00:00+00:00",
                "item_id":     None,
            })
        # Invoke the backfill migration directly.
        with _db.lock:
            _db.backfill_manual_todo_items()
        # Todo should now carry an item_id pointing at the synthesized row.
        with _db.lock:
            row = _db.conn().execute(
                "SELECT item_id FROM todos WHERE id = ?", (tid,)
            ).fetchone()
        assert row["item_id"] == f"manual_{tid}"
        with _db.lock:
            item = _db.get_item(f"manual_{tid}")
        assert item is not None
        assert item["source"] == "manual"
        assert item["title"]  == "legacy manual todo"
        assert item["has_action"] == 1

    def test_backfill_is_idempotent(self, client):
        import db as _db
        with _db.lock:
            tid = _db.insert_todo({
                "description": "another legacy", "priority": "low",
                "is_manual": 1, "done": 0, "status": "open",
                "source": "manual", "title": "", "url": "", "owner": "me",
                "created_at": "2026-04-20T00:00:00+00:00", "item_id": None,
            })
        with _db.lock:
            _db.backfill_manual_todo_items()
            _db.backfill_manual_todo_items()  # second call must be a no-op
        with _db.lock:
            count = _db.conn().execute(
                "SELECT COUNT(*) FROM items WHERE item_id = ?",
                (f"manual_{tid}",),
            ).fetchone()[0]
        assert count == 1

    def test_backfill_skips_non_manual_todos(self, client):
        import db as _db
        # A generated todo already has item_id set; migration should not touch it.
        with _db.lock:
            _db.upsert_item({
                "item_id": "real_item_1", "source": "outlook",
                "title": "real email", "body_preview": "hello",
            })
            _db.insert_todo({
                "description": "generated", "priority": "medium",
                "is_manual": 0, "done": 0, "status": "open",
                "source": "outlook", "title": "real email", "url": "",
                "owner": "me", "created_at": "2026-04-20T00:00:00+00:00",
                "item_id": "real_item_1",
            })
            _db.backfill_manual_todo_items()
            count = _db.conn().execute(
                "SELECT COUNT(*) FROM items WHERE item_id LIKE 'manual_%'"
            ).fetchone()[0]
        assert count == 0


class TestManualTodoCreationSynthesizesItem:
    """POST /todos with no item_id must create a placeholder items row
    so the card opens in the detail panel like a generated card."""

    def test_post_todos_creates_items_row(self, client):
        resp = client.post("/todos", json={
            "description": "prep for Friday meeting",
            "priority":    "high",
            "project_tag": None,
        })
        assert resp.status_code == 200
        doc_id = resp.json()["doc_id"]

        import db as _db
        with _db.lock:
            todo_row = _db.conn().execute(
                "SELECT item_id FROM todos WHERE id = ?", (doc_id,)
            ).fetchone()
        assert todo_row["item_id"] == f"manual_{doc_id}"

        with _db.lock:
            item = _db.get_item(f"manual_{doc_id}")
        assert item is not None
        assert item["source"]     == "manual"
        assert item["title"]      == "prep for Friday meeting"
        assert item["priority"]   == "high"
        assert item["has_action"] == 1
        assert item["category"]   == "task"

    def test_post_todos_with_item_id_does_not_synthesize(self, client):
        import db as _db
        with _db.lock:
            _db.upsert_item({
                "item_id": "real_a", "source": "outlook",
                "title": "real email", "body_preview": "hi",
            })
        resp = client.post("/todos", json={
            "description": "manual child of real email",
            "priority":    "medium",
            "item_id":     "real_a",
        })
        assert resp.status_code == 200
        doc_id = resp.json()["doc_id"]
        with _db.lock:
            todo_row = _db.conn().execute(
                "SELECT item_id FROM todos WHERE id = ?", (doc_id,)
            ).fetchone()
        # Must be the real item_id, not manual_<doc_id>.
        assert todo_row["item_id"] == "real_a"
        # No spurious manual_* row was created.
        with _db.lock:
            count = _db.conn().execute(
                "SELECT COUNT(*) FROM items WHERE item_id LIKE 'manual_%'"
            ).fetchone()[0]
        assert count == 0


class TestPatchAnalysisRichFields:
    """Issue #85: PATCH /analyses/{item_id} must accept the content-level
    fields (summary, urgency_reason, body_preview, goals, key_dates,
    hierarchy, title, user_summary) and record them in user_edited_fields
    so reanalyze preserves them."""

    def _seed(self):
        import db as _db
        with _db.lock:
            _db.upsert_item({
                "item_id":      "edit_me",
                "source":       "manual",
                "title":        "original title",
                "summary":      "original summary",
                "urgency":      "original urgency",
                "body_preview": "original body",
                "goals":        "[]",
                "key_dates":    "[]",
                "hierarchy":    "general",
                "priority":     "medium",
                "category":     "task",
                "has_action":   1,
            })

    def test_patch_accepts_summary(self, client):
        self._seed()
        resp = client.patch("/analyses/edit_me", json={"summary": "new summary"})
        assert resp.status_code == 200
        import db as _db
        with _db.lock:
            row = _db.get_item("edit_me")
        assert row["summary"] == "new summary"
        import json as _json
        assert "summary" in _json.loads(row["user_edited_fields"])

    def test_patch_accepts_body_preview(self, client):
        self._seed()
        resp = client.patch("/analyses/edit_me",
                            json={"body_preview": "free-form notes here"})
        assert resp.status_code == 200
        import db as _db
        with _db.lock:
            row = _db.get_item("edit_me")
        assert row["body_preview"] == "free-form notes here"
        import json as _json
        assert "body_preview" in _json.loads(row["user_edited_fields"])

    def test_patch_accepts_goals_list(self, client):
        self._seed()
        resp = client.patch("/analyses/edit_me",
                            json={"goals": ["draft proposal", "review metrics"]})
        assert resp.status_code == 200
        import db as _db, json as _json
        with _db.lock:
            row = _db.get_item("edit_me")
        assert _json.loads(row["goals"]) == ["draft proposal", "review metrics"]
        assert "goals" in _json.loads(row["user_edited_fields"])

    def test_patch_accepts_key_dates_list(self, client):
        self._seed()
        payload = [
            {"date": "2026-05-01", "description": "submit draft"},
            {"date": "2026-05-15", "description": "review"},
        ]
        resp = client.patch("/analyses/edit_me", json={"key_dates": payload})
        assert resp.status_code == 200
        import db as _db, json as _json
        with _db.lock:
            row = _db.get_item("edit_me")
        assert _json.loads(row["key_dates"]) == payload
        assert "key_dates" in _json.loads(row["user_edited_fields"])

    def test_patch_accepts_title_urgency_hierarchy_user_summary(self, client):
        self._seed()
        resp = client.patch("/analyses/edit_me", json={
            "title":          "new title",
            "urgency_reason": "needs reply today",
            "hierarchy":      "project",
            "user_summary":   "my note",
        })
        assert resp.status_code == 200
        import db as _db, json as _json
        with _db.lock:
            row = _db.get_item("edit_me")
        assert row["title"]        == "new title"
        assert row["urgency"]      == "needs reply today"
        assert row["hierarchy"]    == "project"
        assert row["user_summary"] == "my note"
        edited = set(_json.loads(row["user_edited_fields"]))
        assert {"title", "urgency", "hierarchy", "user_summary"} <= edited

    def test_patch_rejects_unknown_fields(self, client):
        """Body with only unknown keys still returns 400, behaviour unchanged."""
        self._seed()
        resp = client.patch("/analyses/edit_me", json={"bogus": "value"})
        assert resp.status_code == 400
