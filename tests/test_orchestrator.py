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

    def flaky_analyze(item, **_kwargs):
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

    def cancelling_analyze(item, **_kwargs):
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
                    timestamp="2026-03-17T10:00:00+00:00",
                    conversation_id=None):
    """Insert a bare-minimum analysis record for reanalysis tests."""
    analyses.insert({
        "item_id":         item_id,
        "source":          source,
        "title":           f"Item {item_id}",
        "author":          "alice",
        "timestamp":       timestamp,
        "url":             "",
        "body_preview":    f"body of {item_id}",
        "priority":        priority,
        "category":        category,
        "has_action":      False,
        "is_passdown":     is_passdown,
        "project_tag":     None,
        "hierarchy":       "general",
        "to_field":        "",
        "cc_field":        "",
        "is_replied":      False,
        "replied_at":      None,
        "conversation_id": conversation_id,
    })


def _apply_fake_batch_result(item_id, payload):
    """Test helper: invoke the batch-result apply path as if merLLM had
    returned ``payload``. The reanalyze save-logic tests used to drive this
    through a mocked ``analyze()`` call in ``_run_reanalyze``, but reanalyze
    is now batch-only (squire#47) — the save-field logic lives in
    ``_apply_batch_result`` → ``_save_analysis(reanalyze=True)``.
    """
    import json as _json
    with db.lock:
        rec = db.get_item(item_id)
    assert rec is not None
    with patch("situation_manager._spawn_situation_task"), \
         patch("orchestrator.graph.index_item"):
        orchestrator._apply_batch_result(rec, _json.dumps(payload))


def test_reanalyze_apply_reprocesses_stored_items():
    """Items with no existing priority should take the priority from the
    batch-applied LLM payload."""
    _insert_minimal("r1", priority=None)
    _insert_minimal("r2", priority=None)

    payload = {"priority": "high", "category": "fyi"}
    _apply_fake_batch_result("r1", payload)
    _apply_fake_batch_result("r2", payload)

    assert analyses.get(Q.item_id == "r1")["priority"] == "high"
    assert analyses.get(Q.item_id == "r2")["priority"] == "high"


def test_reanalyze_apply_preserves_user_edited_fields():
    """A field explicitly marked as user-edited must survive reanalysis even
    when the LLM returns a different value.  Non-edited fields should update."""
    import json as _json
    _insert_minimal("u1", priority="high")
    with db.lock:
        db.update_item("u1", {"user_edited_fields": _json.dumps(["priority"])})

    _apply_fake_batch_result("u1", {"priority": "low", "category": "fyi"})

    assert analyses.get(Q.item_id == "u1")["priority"] == "high"


def test_reanalyze_apply_updates_non_edited_fields():
    """Fields NOT marked as user-edited should be updated so fresh LLM output
    (with project awareness) takes effect."""
    _insert_minimal("u2", priority="low", category="fyi")

    _apply_fake_batch_result("u2", {"priority": "high", "category": "task",
                                     "task_type": "review"})

    result = analyses.get(Q.item_id == "u2")
    assert result["priority"] == "high"
    assert result["category"] == "task"


def test_reanalyze_apply_deletes_stale_todos_before_reinserting():
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

    _apply_fake_batch_result("t1", {
        "has_action":   True,
        "priority":     "medium",
        "category":     "task",
        "task_type":    "review",
        "action_items": [{"description": "new action", "deadline": None, "owner": "me"}],
        "summary":      "S",
    })

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
    """Passdown items must appear first in the batch submit order regardless of
    their timestamp."""
    _insert_minimal("n1", timestamp="2026-03-17T10:00:00+00:00", is_passdown=False)
    _insert_minimal("n2", timestamp="2026-03-17T11:00:00+00:00", is_passdown=False)
    _insert_minimal("p1", timestamp="2026-03-17T09:00:00+00:00", is_passdown=True)

    submit_order = []

    def recording_submit(prompt):
        # The prompt text contains the item id verbatim since build_prompt
        # embeds it in the context; record by order of invocation.
        submit_order.append(len(submit_order))
        return f"job-{len(submit_order)}"

    fake_ids = iter(["p1", "n2", "n1"])

    def fake_build_prompt(item, **_kwargs):
        # We rely on build_prompt being called in the same iteration order as
        # the submit loop, so recording the item here gives us submit order.
        submit_order.append(item.item_id)
        return f"prompt for {item.item_id}"

    with patch("orchestrator._merllm_batch_available", return_value=True), \
         patch("orchestrator._submit_batch_job", return_value="job-x"), \
         patch("orchestrator.build_prompt", side_effect=fake_build_prompt), \
         patch("orchestrator._ensure_batch_poll_thread"), \
         patch("orchestrator._generate_briefing_bg"):
        _run_reanalyze()

    assert submit_order[0] == "p1"


# ── claim_ingest_items (parsival#58) ──────────────────────────────────────────

def test_claim_ingest_items_returns_all_fresh_ids_on_first_call():
    claimed = orchestrator.claim_ingest_items(["a", "b", "c"])
    assert claimed == {"a", "b", "c"}


def test_claim_ingest_items_skips_ids_already_in_flight():
    """Second /ingest call before the first's background task finishes must
    not re-claim ids still being processed (parsival#58)."""
    orchestrator.claim_ingest_items(["a", "b"])
    claimed = orchestrator.claim_ingest_items(["a", "c"])
    assert claimed == {"c"}


def test_claim_ingest_items_skips_persisted_ids():
    analyses.insert({"item_id": "already-done", "source": "outlook"})
    claimed = orchestrator.claim_ingest_items(["already-done", "new"])
    assert claimed == {"new"}


def test_claim_ingest_items_skips_empty_ids():
    claimed = orchestrator.claim_ingest_items(["", "x"])
    assert claimed == {"x"}


def test_release_ingest_item_allows_reclaim():
    orchestrator.claim_ingest_items(["a"])
    orchestrator.release_ingest_item("a")
    claimed = orchestrator.claim_ingest_items(["a"])
    assert claimed == {"a"}


def test_process_ingest_items_releases_claim_on_success():
    orchestrator.claim_ingest_items(["x1"])
    with patch("orchestrator.analyze", return_value=_analysis("x1")), \
         patch("orchestrator._save_analysis"), \
         patch("orchestrator.graph.index_item"), \
         patch("orchestrator._spawn_situation_task"):
        orchestrator.process_ingest_items([_raw(item_id="x1")])

    assert "x1" not in orchestrator._in_flight_ids


def test_process_ingest_items_releases_claim_on_analyze_exception():
    orchestrator.claim_ingest_items(["boom"])
    with patch("orchestrator.analyze", side_effect=RuntimeError("boom")), \
         patch("orchestrator._spawn_situation_task"):
        orchestrator.process_ingest_items([_raw(item_id="boom")])

    assert "boom" not in orchestrator._in_flight_ids


def test_process_ingest_items_releases_remaining_claims_on_cancel():
    """If cancellation fires mid-loop, unprocessed ids must leave the
    in-flight set so a future /ingest call can re-queue them."""
    orchestrator.claim_ingest_items(["x1", "x2", "x3"])

    def cancelling_analyze(item, **_kwargs):
        scan_state["cancelled"] = True
        return _analysis(item.item_id)

    try:
        with patch("orchestrator.analyze", side_effect=cancelling_analyze), \
             patch("orchestrator._save_analysis"), \
             patch("orchestrator.graph.index_item"), \
             patch("orchestrator._spawn_situation_task"):
            orchestrator.process_ingest_items([
                _raw(item_id="x1"),
                _raw(item_id="x2"),
                _raw(item_id="x3"),
            ])
        assert orchestrator._in_flight_ids == set()
    finally:
        scan_state["cancelled"] = False


# ── Thread-aware analysis (parsival#79) ───────────────────────────────────────

def _raw_conv(item_id, conversation_id, timestamp,
              body="body text", source="outlook"):
    return RawItem(
        source=source, item_id=item_id, title=f"Item {item_id}",
        body=body, url="", author="alice@example.com", timestamp=timestamp,
        metadata={"conversation_id": conversation_id},
    )


def _analysis_with_action(item_id, conversation_id, description,
                          timestamp="2026-04-01T10:00:00+00:00"):
    a = Analysis(
        item_id=item_id, source="outlook", title=f"Item {item_id}",
        author="alice", timestamp=timestamp, url="",
        has_action=True, priority="medium", category="task",
        action_items=[ActionItem(description=description, deadline=None, owner="me")],
        summary="S", urgency_reason=None,
    )
    a.conversation_id = conversation_id
    return a


def test_process_ingest_items_sorts_and_passes_thread_todos_within_conversation():
    """Within a conversation_id, items are processed oldest-first and each
    analyze() call receives thread_todos built from earlier messages'
    persisted todos (parsival#79)."""
    orchestrator.claim_ingest_items(["m1", "m2", "m3"])

    calls = []

    def fake_analyze(item, **kwargs):
        calls.append({
            "item_id": item.item_id,
            "thread_todos": list(kwargs.get("thread_todos") or []),
        })
        return _analysis_with_action(
            item.item_id, "conv-A",
            f"Task from {item.item_id}", timestamp=item.timestamp,
        )

    # Input order is intentionally shuffled — orchestrator must sort.
    items = [
        _raw_conv("m3", "conv-A", "2026-04-01T12:00:00+00:00"),
        _raw_conv("m1", "conv-A", "2026-04-01T10:00:00+00:00"),
        _raw_conv("m2", "conv-A", "2026-04-01T11:00:00+00:00"),
    ]

    with patch("orchestrator.analyze", side_effect=fake_analyze), \
         patch("orchestrator.graph.index_item"), \
         patch("orchestrator._spawn_situation_task"):
        orchestrator.process_ingest_items(items)

    assert [c["item_id"] for c in calls] == ["m1", "m2", "m3"]
    assert calls[0]["thread_todos"] == []
    call2_descs = {t["description"] for t in calls[1]["thread_todos"]}
    assert "Task from m1" in call2_descs
    call3_descs = {t["description"] for t in calls[2]["thread_todos"]}
    assert {"Task from m1", "Task from m2"}.issubset(call3_descs)


def test_process_ingest_items_isolates_thread_todos_between_conversations():
    """Conversation A's todos must not leak into conversation B's prompt."""
    orchestrator.claim_ingest_items(["a1", "b1"])

    calls = []

    def fake_analyze(item, **kwargs):
        calls.append({
            "item_id": item.item_id,
            "thread_todos": list(kwargs.get("thread_todos") or []),
        })
        conv = item.metadata.get("conversation_id", "")
        return _analysis_with_action(
            item.item_id, conv, f"Task from {item.item_id}",
            timestamp=item.timestamp,
        )

    with patch("orchestrator.analyze", side_effect=fake_analyze), \
         patch("orchestrator.graph.index_item"), \
         patch("orchestrator._spawn_situation_task"):
        orchestrator.process_ingest_items([
            _raw_conv("a1", "conv-A", "2026-04-01T10:00:00+00:00"),
            _raw_conv("b1", "conv-B", "2026-04-01T10:00:00+00:00"),
        ])

    for c in calls:
        assert c["thread_todos"] == [], (
            f"{c['item_id']} saw thread_todos from another conversation"
        )


def test_run_reanalyze_fetches_thread_todos_for_conversation_items():
    """Bulk reanalyze must rehydrate prior-message thread_todos for items
    that belong to a conversation, scoped to before the item's timestamp
    so an item does not self-suppress on its own todos (parsival#79)."""
    _insert_minimal(
        "msg-1", source="outlook",
        timestamp="2026-04-01T09:00:00+00:00",
        conversation_id="conv-X",
    )
    _insert_minimal(
        "msg-2", source="outlook",
        timestamp="2026-04-01T11:00:00+00:00",
        conversation_id="conv-X",
    )
    todos.insert({
        "item_id": "msg-1", "source": "outlook", "title": "T", "url": "",
        "description": "Review spec", "deadline": None, "owner": "me",
        "priority": "medium", "done": False,
        "created_at": "2026-04-01T09:05:00+00:00",
    })

    captured = []

    def fake_build_prompt(item, **kwargs):
        captured.append({
            "item_id": item.item_id,
            "thread_todos": list(kwargs.get("thread_todos") or []),
        })
        return f"prompt for {item.item_id}"

    with patch("orchestrator._merllm_batch_available", return_value=True), \
         patch("orchestrator._submit_batch_job", return_value="job-x"), \
         patch("orchestrator.build_prompt", side_effect=fake_build_prompt), \
         patch("orchestrator._ensure_batch_poll_thread"), \
         patch("orchestrator._generate_briefing_bg"):
        _run_reanalyze()

    by_id = {c["item_id"]: c for c in captured}
    assert by_id["msg-1"]["thread_todos"] == []
    descs_msg2 = [t["description"] for t in by_id["msg-2"]["thread_todos"]]
    assert "Review spec" in descs_msg2


def test_process_ingest_items_empty_thread_todos_for_standalone_items():
    """Items without a conversation_id (Slack DMs, GitHub, Jira, etc.) must
    always receive empty thread_todos — they have no thread to look up."""
    orchestrator.claim_ingest_items(["s1"])

    captured = {}

    def fake_analyze(item, **kwargs):
        captured["thread_todos"] = kwargs.get("thread_todos")
        return _analysis("s1")

    slack_item = RawItem(
        source="slack", item_id="s1", title="DM",
        body="hi", url="", author="alice",
        timestamp="2026-04-01T10:00:00+00:00", metadata={},
    )

    with patch("orchestrator.analyze", side_effect=fake_analyze), \
         patch("orchestrator._save_analysis"), \
         patch("orchestrator.graph.index_item"), \
         patch("orchestrator._spawn_situation_task"):
        orchestrator.process_ingest_items([slack_item])

    assert captured["thread_todos"] == []


def test_run_reanalyze_aborts_when_merllm_unavailable():
    """squire#47: if merLLM is unreachable, reanalyze must refuse to run rather
    than silently falling back to the non-durable /api/generate path."""
    _insert_minimal("a1")
    _insert_minimal("a2")

    with patch("orchestrator._merllm_batch_available", return_value=False), \
         patch("orchestrator._submit_batch_job") as submit, \
         patch("orchestrator._generate_briefing_bg"):
        _run_reanalyze()

    submit.assert_not_called()
    assert "unreachable" in orchestrator._scan_state["message"].lower()
    # Items must stay untouched (no batch_job_id assigned).
    for iid in ("a1", "a2"):
        rec = analyses.get(Q.item_id == iid)
        assert not rec.get("batch_job_id")


def test_apply_batch_result_preserves_delegated_owner():
    """Regression guard for issue #83 on the BATCH path: when the LLM
    response assigns owner to another person, the stored todo must have
    status='assigned' and assigned_to resolved from the To header.  Both
    paths (sync analyze() + batch _apply_batch_result) funnel through
    build_analysis_from_llm_json, which no longer runs postprocess_action_items."""
    # Seed the item row _raw_item_from_record will hydrate
    db.upsert_item({
        "item_id":          "batch-rv17-83",
        "source":           "outlook",
        "direction":        "received",
        "title":            "RE: 4/15/2026",
        "author":           "Alex Washington <alex@dubscontrols.com>",
        "timestamp":        "2026-04-17T08:42:35",
        "url":              "",
        "body_preview":     "Logan, did we get started on CV/LiDAR testing?",
        "to_field":         "Logan Souza <Logan.Souza@universalorlando.com>; Reid Hall <reid.hall@prismsystems.com>",
        "cc_field":         "",
        "hierarchy":        "general",
        "conversation_id":  "conv-2",
    })

    _apply_fake_batch_result("batch-rv17-83", {
        "category":    "task",
        "priority":    "medium",
        "has_action":  True,
        "summary":     "Follow up on CV/LiDAR testing",
        "hierarchy":   "project",
        "action_items": [
            {"description": "Follow up on whether CV/LiDAR testing started",
             "deadline": None, "owner": "Logan Souza"}
        ],
        "information_items": [],
        "goals": [], "key_dates": [],
    })

    rows = db.get_todos_for_item("batch-rv17-83")
    assert len(rows) == 1, rows
    t = rows[0]
    assert t["owner"] == "Logan Souza"
    assert t["status"] == "assigned", t
    assert t["assigned_to"] == "logan.souza@universalorlando.com"


def test_run_reanalyze_skips_manual_items(client, monkeypatch):
    """Manual cards have no source message; they must be excluded from the
    reanalyze batch-submit loop or merLLM wastes a slot on empty input."""
    import db as _db
    import orchestrator as _orc

    # Seed a real item plus a synthesized manual item.
    with _db.lock:
        _db.upsert_item({
            "item_id": "real_t3", "source": "outlook",
            "title": "real email", "body_preview": "hi",
            "timestamp": "2026-04-20T00:00:00+00:00",
        })
        _db.upsert_item({
            "item_id": "manual_t3", "source": "manual",
            "title": "my manual card", "body_preview": "",
            "timestamp": "2026-04-20T00:00:00+00:00",
        })

    submitted: list[str] = []

    def fake_submit_batch_job(prompt: str) -> str:
        return "fake-job-id"

    def fake_set_batch_job_id(item_id: str, job_id: str) -> None:
        submitted.append(item_id)

    monkeypatch.setattr(_orc, "_merllm_batch_available", lambda: True)
    monkeypatch.setattr(_orc, "_ensure_batch_poll_thread", lambda: None)
    monkeypatch.setattr(_orc, "_submit_batch_job", fake_submit_batch_job)
    monkeypatch.setattr(_db, "set_batch_job_id", fake_set_batch_job_id)

    _orc.run_reanalyze()

    assert "real_t3"   in submitted
    assert "manual_t3" not in submitted
