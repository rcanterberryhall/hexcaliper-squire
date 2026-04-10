"""tests/test_orchestrator.py — Unit tests for orchestrator.run_scan and run_reanalyze.

Validates that items flow through the pipeline and land in the database
correctly. The existing scan tests in test_app.py only check HTTP status
codes and conflict detection; these tests assert on what gets persisted.
"""
from unittest.mock import patch, MagicMock

import db
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

def _insert_minimal(item_id, source="jira", priority=None, category=None,
                    is_passdown=False,
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
        "category":    category,
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
    """A field explicitly marked as user-edited must survive reanalysis even
    when the LLM returns a different value.  Non-edited fields should update."""
    import json as _json
    _insert_minimal("u1", priority="high")
    # Mark priority as user-edited so it persists through reanalysis
    with db.lock:
        db.update_item("u1", {"user_edited_fields": _json.dumps(["priority"])})

    with patch("orchestrator.analyze", return_value=_analysis("u1", priority="low")), \
         patch("situation_manager._spawn_situation_task"):
        _run_reanalyze()

    assert analyses.get(Q.item_id == "u1")["priority"] == "high"


def test_run_reanalyze_updates_non_edited_fields():
    """Fields NOT marked as user-edited should be updated by reanalysis
    so fresh LLM output (with project awareness) takes effect."""
    _insert_minimal("u2", priority="low", category="fyi")

    with patch("orchestrator.analyze", return_value=_analysis("u2", priority="high", category="task")), \
         patch("situation_manager._spawn_situation_task"):
        _run_reanalyze()

    result = analyses.get(Q.item_id == "u2")
    assert result["priority"] == "high"
    assert result["category"] == "task"


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


# ── _poll_batch_once (batch result application) ──────────────────────────────

def _seed_pending_batch(item_id="b1", job_id="job1", source="outlook",
                        title="Pending item", to_field="user@co.com",
                        cc_field=""):
    """Insert an item with a batch_job_id set, mimicking a row that submitted
    its prompt to merLLM and is awaiting the result."""
    analyses.insert({
        "item_id":      item_id,
        "source":       source,
        "title":        title,
        "author":       "boss@co.com",
        "timestamp":    "2026-04-10T10:00:00+00:00",
        "url":          "",
        "body_preview": "please ship the release today",
        "priority":     "low",
        "category":     "fyi",
        "has_action":   False,
        "is_passdown":  False,
        "project_tag":  None,
        "hierarchy":    "general",
        "to_field":     to_field,
        "cc_field":     cc_field,
        "is_replied":   False,
        "replied_at":   None,
        "batch_job_id": job_id,
    })


def _merllm_ok_response(payload: dict) -> MagicMock:
    import json as _json
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"result": _json.dumps(payload)}
    m.raise_for_status.return_value = None
    return m


def test_poll_batch_once_applies_completed_result():
    """Happy path: a 200 response with valid LLM JSON updates the item,
    clears batch_job_id, and persists the new analysis fields."""
    _seed_pending_batch(item_id="b1", job_id="job-success")

    payload = {
        "has_action": True, "priority": "high", "category": "task",
        "task_type": "review",
        "action_items": [{"description": "Ship the release", "deadline": None, "owner": "me"}],
        "summary": "Ship the release", "urgency_reason": "deadline today",
    }

    with patch("orchestrator.http_requests.get", return_value=_merllm_ok_response(payload)), \
         patch("orchestrator._spawn_situation_task"):
        orchestrator._poll_batch_once()

    row = analyses.get(Q.item_id == "b1")
    assert row is not None
    assert row["batch_job_id"] is None  # cleared on success
    assert row["priority"] == "high"
    assert row["category"] == "task"
    assert row["has_action"] in (True, 1)


def test_poll_batch_once_clears_job_id_on_malformed_payload():
    """Critical regression guard for #26: a malformed result must NOT wedge
    the item forever.  The job id is cleared so the next reanalyze run can
    pick the item back up."""
    _seed_pending_batch(item_id="b2", job_id="job-bad")

    bad = MagicMock()
    bad.status_code = 200
    bad.json.return_value = {"result": "{not valid json"}
    bad.raise_for_status.return_value = None

    with patch("orchestrator.http_requests.get", return_value=bad), \
         patch("orchestrator._spawn_situation_task"):
        # Even with bad JSON, the helper now treats it as defaults — so this
        # is actually a *successful* apply with empty fields.  Either way the
        # item must not be left wedged with the old job id.
        orchestrator._poll_batch_once()

    row = analyses.get(Q.item_id == "b2")
    assert row["batch_job_id"] is None


def test_poll_batch_once_clears_job_id_when_save_fails():
    """If the save/index/spawn step itself raises (not the JSON parse), the
    exception handler must still clear batch_job_id to unstick the item."""
    _seed_pending_batch(item_id="b3", job_id="job-save-fail")

    payload = {"category": "task", "priority": "medium"}

    with patch("orchestrator.http_requests.get", return_value=_merllm_ok_response(payload)), \
         patch("orchestrator.graph.index_item", side_effect=RuntimeError("graph down")), \
         patch("orchestrator._spawn_situation_task"):
        orchestrator._poll_batch_once()

    row = analyses.get(Q.item_id == "b3")
    assert row["batch_job_id"] is None  # unstuck even on save failure


def test_poll_batch_once_clears_job_id_on_404():
    """merLLM doesn't know the job (e.g. its DB was wiped) → clear so the
    item is picked up by the next direct reanalyze run."""
    _seed_pending_batch(item_id="b4", job_id="job-missing")

    not_found = MagicMock()
    not_found.status_code = 404
    not_found.json.return_value = {"detail": "not found"}

    with patch("orchestrator.http_requests.get", return_value=not_found):
        orchestrator._poll_batch_once()

    row = analyses.get(Q.item_id == "b4")
    assert row["batch_job_id"] is None


def test_poll_batch_once_keeps_job_id_when_still_running():
    """409 with detail containing 'queued'/'running' → keep waiting."""
    _seed_pending_batch(item_id="b5", job_id="job-running")

    running = MagicMock()
    running.status_code = 409
    running.json.return_value = {"detail": "job is still running"}

    with patch("orchestrator.http_requests.get", return_value=running):
        orchestrator._poll_batch_once()

    row = analyses.get(Q.item_id == "b5")
    assert row["batch_job_id"] == "job-running"  # still pending


def test_poll_batch_once_clears_job_id_on_409_failed():
    """409 with detail containing 'failed' → merLLM gave up; clear so the
    next reanalyze run picks the item back up directly."""
    _seed_pending_batch(item_id="b6", job_id="job-failed")

    failed = MagicMock()
    failed.status_code = 409
    failed.json.return_value = {"detail": "job failed"}

    with patch("orchestrator.http_requests.get", return_value=failed):
        orchestrator._poll_batch_once()

    row = analyses.get(Q.item_id == "b6")
    assert row["batch_job_id"] is None


def test_poll_batch_once_uses_shared_helper(monkeypatch):
    """Smoke test: confirm the batch path actually calls
    agent.build_analysis_from_llm_json (proves the import is wired up
    correctly and we never silently fall back to the old hand-rolled path)."""
    _seed_pending_batch(item_id="b7", job_id="job-helper")

    import agent
    real_helper = agent.build_analysis_from_llm_json
    calls = []

    def spy(item, text, *, scope_info):
        calls.append((item.item_id, scope_info["scope"]))
        return real_helper(item, text, scope_info=scope_info)

    monkeypatch.setattr(agent, "build_analysis_from_llm_json", spy)

    payload = {"category": "task", "priority": "medium",
               "action_items": [{"description": "do thing", "owner": "me"}]}

    with patch("orchestrator.http_requests.get", return_value=_merllm_ok_response(payload)), \
         patch("orchestrator._spawn_situation_task"):
        orchestrator._poll_batch_once()

    assert len(calls) == 1
    assert calls[0][0] == "b7"


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
