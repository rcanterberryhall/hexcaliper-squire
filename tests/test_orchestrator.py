"""tests/test_orchestrator.py — Unit tests for orchestrator.run_scan and run_reanalyze.

Validates that items flow through the pipeline and land in the database
correctly. The existing scan tests in test_app.py only check HTTP status
codes and conflict detection; these tests assert on what gets persisted.
"""
from unittest.mock import patch, MagicMock

import orchestrator
from app import (
    analyses,
    todos,
    scan_logs,
    scan_state,
    Q,
)
from models import RawItem, Analysis, ActionItem

_run_scan     = orchestrator.run_scan
_run_reanalyze = orchestrator.run_reanalyze


def _raw(source="outlook", item_id="x1", title="Test item"):
    return RawItem(
        source=source,
        item_id=item_id,
        title=title,
        body="body text",
        url="",
        author="alice@example.com",
        timestamp="2026-03-17T10:00:00+00:00",
    )


def _analysis(item_id="x1", source="outlook", has_action=False,
              priority="medium", category="fyi"):
    return Analysis(
        item_id=item_id, source=source, title="Test item",
        author="alice", timestamp="2026-03-17T10:00:00+00:00",
        url="", has_action=has_action, priority=priority,
        category=category, action_items=[], summary="S",
        urgency_reason=None,
    )


def _all_connectors_patched(outlook_items=None, github_items=None,
                            slack_items=None, jira_items=None,
                            teams_items=None):
    """Return a context-manager stack that patches all five connector fetch()
    methods.  Unspecified connectors return []."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("orchestrator.connector_outlook.fetch",
                              return_value=outlook_items or []))
    stack.enter_context(patch("orchestrator.connector_github.fetch",
                              return_value=github_items or []))
    stack.enter_context(patch("orchestrator.connector_slack.fetch",
                              return_value=slack_items or []))
    stack.enter_context(patch("orchestrator.connector_jira.fetch",
                              return_value=jira_items or []))
    stack.enter_context(patch("orchestrator.connector_teams.fetch",
                              return_value=teams_items or []))
    return stack


# ── _run_scan ─────────────────────────────────────────────────────────────────

def test_run_scan_saves_analysis_to_db():
    with _all_connectors_patched(outlook_items=[_raw()]), \
         patch("orchestrator.analyze", return_value=_analysis()), \
         patch("situation_manager._spawn_situation_task"):
        _run_scan(["outlook"])

    assert analyses.get(Q.item_id == "x1") is not None
    assert len(scan_logs.all()) == 1


def test_run_scan_creates_todo_for_action_item():
    action = ActionItem(description="Review the PR", deadline=None, owner="me")
    result = Analysis(
        item_id="x1", source="outlook", title="Test item",
        author="alice", timestamp="2026-03-17T10:00:00+00:00",
        url="", has_action=True, priority="medium", category="task",
        action_items=[action], summary="S", urgency_reason=None,
    )

    with _all_connectors_patched(outlook_items=[_raw()]), \
         patch("orchestrator.analyze", return_value=result), \
         patch("situation_manager._spawn_situation_task"):
        _run_scan(["outlook"])

    todo = todos.get(Q.item_id == "x1")
    assert todo is not None
    assert todo["description"] == "Review the PR"


def test_run_scan_writes_log_on_success():
    with _all_connectors_patched(github_items=[_raw(source="github")]), \
         patch("orchestrator.analyze", return_value=_analysis(source="github")), \
         patch("situation_manager._spawn_situation_task"):
        _run_scan(["github"])

    logs = scan_logs.all()
    assert len(logs) == 1
    assert logs[0]["status"] == "success"
    assert logs[0]["sources"] == "github"


def test_run_scan_handles_analyze_exception_gracefully():
    """An exception during analysis of one item must not crash the loop; the
    remaining items should still be processed and scan_state["running"] must
    be False when the function returns."""
    items = [_raw(item_id="x1"), _raw(item_id="x2")]
    call_count = {"n": 0}

    def flaky_analyze(item):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("timeout")
        return _analysis(item.item_id)

    with _all_connectors_patched(outlook_items=items), \
         patch("orchestrator.analyze", side_effect=flaky_analyze), \
         patch("situation_manager._spawn_situation_task"):
        _run_scan(["outlook"])

    assert scan_state["running"] is False
    assert analyses.get(Q.item_id == "x2") is not None


def test_run_scan_respects_cancellation():
    """Setting scan_state["cancelled"] during analysis must stop mid-loop,
    write a 'cancelled' log entry, and leave fewer than all items saved."""
    items = [_raw(item_id="x1"), _raw(item_id="x2"), _raw(item_id="x3")]

    def cancelling_analyze(item):
        scan_state["cancelled"] = True
        return _analysis(item.item_id)

    try:
        with _all_connectors_patched(outlook_items=items), \
             patch("orchestrator.analyze", side_effect=cancelling_analyze), \
             patch("situation_manager._spawn_situation_task"):
            _run_scan(["outlook"])

        saved = analyses.all()
        assert len(saved) < 3

        logs = scan_logs.all()
        assert len(logs) == 1
        assert logs[0]["status"] == "cancelled"
    finally:
        scan_state["cancelled"] = False


def test_run_scan_sets_running_false_after_completion():
    with _all_connectors_patched(), \
         patch("orchestrator.analyze", return_value=_analysis()), \
         patch("situation_manager._spawn_situation_task"):
        _run_scan(["slack"])

    assert scan_state["running"] is False


# ── _run_reanalyze ────────────────────────────────────────────────────────────

def _insert_minimal(item_id, source="jira", priority=None, is_passdown=False,
                    timestamp="2026-03-17T10:00:00+00:00"):
    """Insert a bare-minimum analysis record for reanalysis tests."""
    analyses.insert({
        "item_id":     item_id,
        "source":      source,
        "title":       f"Item {item_id}",
        "author":      "alice",
        "timestamp":   timestamp,
        "url":         "",
        "body_preview": f"body of {item_id}",
        "priority":    priority,
        "category":    None,
        "has_action":  False,
        "is_passdown": is_passdown,
        "project_tag": None,
        "hierarchy":   "general",
        "to_field":    "",
        "cc_field":    "",
        "is_replied":  False,
        "replied_at":  None,
    })


def test_run_reanalyze_reprocesses_stored_items():
    """Items with no existing priority (None) should take the priority from the
    fresh analysis result after _run_reanalyze."""
    _insert_minimal("r1", priority=None)
    _insert_minimal("r2", priority=None)

    def high_priority_analyze(item):
        return _analysis(item.item_id, priority="high")

    with patch("orchestrator.analyze", side_effect=high_priority_analyze), \
         patch("situation_manager._spawn_situation_task"):
        _run_reanalyze()

    assert analyses.get(Q.item_id == "r1")["priority"] == "high"
    assert analyses.get(Q.item_id == "r2")["priority"] == "high"


def test_run_reanalyze_preserves_user_edited_fields():
    """An existing priority value (simulating a manual override) must survive
    reanalysis even when the LLM returns a different priority."""
    _insert_minimal("u1", priority="high")

    with patch("orchestrator.analyze", return_value=_analysis("u1", priority="low")), \
         patch("situation_manager._spawn_situation_task"):
        _run_reanalyze()

    assert analyses.get(Q.item_id == "u1")["priority"] == "high"


def test_run_reanalyze_deletes_stale_todos_before_reinserting():
    """_save_analysis(reanalyze=True) should remove the old todo and insert a
    new one with the updated description — not accumulate duplicates."""
    import json as _json
    analyses.insert({
        "item_id":      "t1",
        "source":       "jira",
        "title":        "Item t1",
        "author":       "alice",
        "timestamp":    "2026-03-17T10:00:00+00:00",
        "url":          "",
        "body_preview": "body",
        "has_action":   True,
        "priority":     "medium",
        "category":     "task",
        "action_items": _json.dumps([
            {"description": "old action", "deadline": None, "owner": "me"}
        ]),
        "is_passdown":  False,
        "project_tag":  None,
        "hierarchy":    "general",
        "to_field":     "",
        "cc_field":     "",
        "is_replied":   False,
        "replied_at":   None,
    })
    todos.insert({
        "item_id":     "t1",
        "source":      "jira",
        "title":       "Item t1",
        "url":         "",
        "description": "old action",
        "deadline":    None,
        "owner":       "me",
        "priority":    "medium",
        "done":        False,
        "created_at":  "2026-03-17T10:00:00+00:00",
    })

    new_action = ActionItem(description="new action", deadline=None, owner="me")
    new_result = Analysis(
        item_id="t1", source="jira", title="Item t1",
        author="alice", timestamp="2026-03-17T10:00:00+00:00",
        url="", has_action=True, priority="medium", category="task",
        action_items=[new_action], summary="S", urgency_reason=None,
    )

    with patch("orchestrator.analyze", return_value=new_result), \
         patch("situation_manager._spawn_situation_task"):
        _run_reanalyze()

    all_todos = todos.all()
    assert len(all_todos) == 1
    assert all_todos[0]["description"] == "new action"


def test_run_reanalyze_sorts_passdowns_first():
    """Passdown items must appear first in the analysis call order regardless of
    their timestamp."""
    _insert_minimal("n1", timestamp="2026-03-17T10:00:00+00:00", is_passdown=False)
    _insert_minimal("n2", timestamp="2026-03-17T11:00:00+00:00", is_passdown=False)
    _insert_minimal("p1", timestamp="2026-03-17T09:00:00+00:00", is_passdown=True)

    call_order = []

    def recording_analyze(item):
        call_order.append(item.item_id)
        return _analysis(item.item_id)

    with patch("orchestrator.analyze", side_effect=recording_analyze), \
         patch("situation_manager._spawn_situation_task"):
        _run_reanalyze()

    assert call_order[0] == "p1"
