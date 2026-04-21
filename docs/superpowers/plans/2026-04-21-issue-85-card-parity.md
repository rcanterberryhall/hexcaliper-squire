# Issue #85 — Manual + Generated Card Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give manual todos full editable-card parity with LLM-generated cards (body notes, goals, key dates, linked tasks), and make those same rich fields editable on generated cards so the user can correct LLM misses.

**Architecture:** Synthesize a placeholder `items` row (`source='manual'`, `item_id='manual_<int>'`) whenever a manual todo is created. Then the existing `openDetail()` card panel, `GET/PATCH /analyses/{item_id}`, and linked-todos join all work for both card types uniformly. Widen `PATCH /analyses/{item_id}` to accept the rich fields (summary, urgency_reason, body_preview, goals, key_dates, hierarchy, title, user_summary) and add them to `user_edited_fields` so reanalyze preserves user edits. Guard `run_reanalyze` against `source='manual'` items. UI gains inline editors for goals / key_dates / notes / linked-todo CRUD, and the “Add action” minimal form becomes “New card” that opens the full detail panel on a freshly-synthesized manual card.

**Tech Stack:** FastAPI (Python), SQLite (`api/db.py`), vanilla JS single-page app (`web/page/index.html`), pytest + FastAPI TestClient.

---

## File Structure

**Backend (modified):**
- `api/db.py` — add one migration block in `_migrate_schema` that backfills `items` rows for orphaned manual todos. No new tables.
- `api/app.py`
  - `POST /todos` (≈1686-1735): synthesize a placeholder `items` row when `item_id` is not supplied.
  - `PATCH /analyses/{item_id}` (≈918-1049): widen accepted fields, update `_editable_fields`.
  - No new endpoints — existing surface covers everything.
- `api/orchestrator.py`
  - `run_reanalyze` (≈494): skip `source='manual'` items.

**Frontend (modified, single file):**
- `web/page/index.html`
  - `makeTodo` (≈2408): ensure click target resolves via synthesized item_id (post-migration this is guaranteed).
  - `openDetail` (≈4045): branch rendering for `source='manual'` (suppress From/To/CC, hide source-link footer).
  - New inline editors for `goals`, `key_dates`, and `body_preview` / `user_summary`.
  - New “Add task” button in card Tasks section (POST /todos with `item_id`).
  - Replace the inline `addActionForm` with a “New card” button that POSTs `/todos` (no item_id, empty description placeholder) then opens the detail panel on the new manual card.

**Tests (modified):**
- `tests/test_app.py` — new tests for the widened PATCH, POST-synthesizes-items behaviour, migration behaviour.
- `tests/test_orchestrator.py` — one test that `run_reanalyze` skips manual items.

---

## Task 1: Backfill migration — synthesize `items` rows for existing orphan manual todos

**Files:**
- Modify: `api/db.py` (the `_migrate_schema` function, ~line 131)
- Test: `tests/test_app.py` (new `TestManualTodoMigration` class)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_app.py::TestManualTodoMigration -v`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'backfill_manual_todo_items'`

- [ ] **Step 3: Add the backfill function and migration hook**

In `api/db.py`, add after the existing `_migrate_schema` body (after the last migration block, before the closing `def` of another function):

```python
def backfill_manual_todo_items() -> int:
    """Create placeholder items rows for manual todos that predate the
    synthesized-item model (is_manual=1, item_id IS NULL).

    Each orphan todo gets item_id='manual_<todo_id>' on both the todo and
    a synthesized items row whose title/body seed from the todo description
    so the detail panel has something to render.

    Idempotent: runs only against todos still missing item_id.

    Returns the number of rows backfilled.
    """
    c = conn()
    orphans = c.execute(
        "SELECT id, description, priority, project_tag, created_at "
        "FROM todos WHERE is_manual = 1 AND (item_id IS NULL OR item_id = '')"
    ).fetchall()
    for row in orphans:
        new_iid = f"manual_{row['id']}"
        desc    = row["description"] or ""
        upsert_item({
            "item_id":      new_iid,
            "source":       "manual",
            "direction":    "received",
            "title":        desc[:200],
            "author":       "",
            "timestamp":    row["created_at"] or _now_iso(),
            "url":          "",
            "has_action":   1,
            "priority":     row["priority"] or "medium",
            "category":     "task",
            "summary":      "",
            "action_items": "[]",
            "hierarchy":    "general",
            "project_tag":  row["project_tag"],
            "goals":        "[]",
            "key_dates":    "[]",
            "information_items": "[]",
            "body_preview": "",
            "references":   "[]",
        })
        c.execute("UPDATE todos SET item_id = ? WHERE id = ?", (new_iid, row["id"]))
    return len(orphans)
```

Then, in `_migrate_schema`, after the existing migration blocks (after the contacts migration at ~line 149-160, find the last `if "..." not in ..._cols:` block), add:

```python
    # Backfill synthesized items rows for legacy manual todos (issue #85).
    # Running inside _migrate_schema ensures every live DB catches up exactly
    # once at next startup; subsequent runs are no-ops because orphans have
    # already been linked.
    backfill_manual_todo_items()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_app.py::TestManualTodoMigration -v`
Expected: PASS (3 tests).

Also run the full suite to confirm nothing regressed:

Run: `pytest -q`
Expected: all previous tests still pass (460+ passing, 0 failing).

- [ ] **Step 5: Commit**

```bash
git add api/db.py tests/test_app.py
git commit -m "feat(db): backfill synthesized items rows for legacy manual todos (#85)"
```

---

## Task 2: `POST /todos` synthesizes items row when no item_id supplied

**Files:**
- Modify: `api/app.py` around `create_todo` (lines 1686-1735)
- Test: `tests/test_app.py` (append to the existing manual-todo area)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_app.py::TestManualTodoCreationSynthesizesItem -v`
Expected: FAIL — the first test fails because `item_id` is never written on the todo (remains None) and no items row exists.

- [ ] **Step 3: Implement synthesis in `create_todo`**

In `api/app.py`, replace the body of `create_todo` (lines 1700-1735) with:

```python
    description = (body.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    allowed_priorities = {"high", "medium", "low"}
    priority = body.get("priority", "medium")
    if priority not in allowed_priorities:
        priority = "medium"
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "description": description,
        "priority":    priority,
        "is_manual":   1,
        "done":        0,
        "status":      "open",
        "created_at":  now,
        "source":      "manual",
        "title":       "",
        "url":         "",
        "owner":       "me",
    }
    if body.get("deadline"):
        data["deadline"] = body["deadline"]
    if body.get("project_tag"):
        data["project_tag"] = body["project_tag"]

    linked_item_id = body.get("item_id")
    if linked_item_id:
        data["item_id"] = linked_item_id
        with db.lock:
            item = db.get_item(linked_item_id)
        if item:
            data["source"]      = item.get("source", "manual")
            data["title"]       = item.get("title", "")
            data["url"]         = item.get("url", "")
            data["project_tag"] = data.get("project_tag") or item.get("project_tag")
        with db.lock:
            doc_id = db.insert_todo(data)
        return {"ok": True, "doc_id": doc_id}

    # Manual card with no linked item — synthesize a placeholder items row so
    # the detail panel, PATCH /analyses, and reanalyze-preservation mechanism
    # all work for it uniformly.
    with db.lock:
        doc_id = db.insert_todo(data)
        new_item_id = f"manual_{doc_id}"
        db.upsert_item({
            "item_id":      new_item_id,
            "source":       "manual",
            "direction":    "received",
            "title":        description[:200],
            "author":       "",
            "timestamp":    now,
            "url":          "",
            "has_action":   1,
            "priority":     priority,
            "category":     "task",
            "summary":      "",
            "action_items": "[]",
            "hierarchy":    "general",
            "project_tag":  data.get("project_tag"),
            "goals":        "[]",
            "key_dates":    "[]",
            "information_items": "[]",
            "body_preview": "",
            "references":   "[]",
        })
        db.update_todo(doc_id, {"item_id": new_item_id})
    return {"ok": True, "doc_id": doc_id, "item_id": new_item_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_app.py::TestManualTodoCreationSynthesizesItem -v`
Expected: PASS (2 tests).

Then full suite:

Run: `pytest -q`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add api/app.py tests/test_app.py
git commit -m "feat(todos): synthesize items row for manual POST /todos (#85)"
```

---

## Task 3: Guard `run_reanalyze` against `source='manual'` items

**Files:**
- Modify: `api/orchestrator.py` (`run_reanalyze`, ~line 514)
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orchestrator.py`:

```python
def test_run_reanalyze_skips_manual_items(client, monkeypatch):
    """Manual cards have no source message; they must be excluded from the
    reanalyze batch-submit loop or merLLM wastes a slot on empty input."""
    import db as _db
    import orchestrator as _orc

    # Seed a real item plus a synthesized manual item.
    with _db.lock:
        _db.upsert_item({
            "item_id": "real_1", "source": "outlook",
            "title": "real email", "body_preview": "hi",
            "timestamp": "2026-04-20T00:00:00+00:00",
        })
        _db.upsert_item({
            "item_id": "manual_1", "source": "manual",
            "title": "my manual card", "body_preview": "",
            "timestamp": "2026-04-20T00:00:00+00:00",
        })

    submitted: list[str] = []

    def fake_submit_batch(rec):
        submitted.append(rec["item_id"])
        return "fake-batch-id"

    monkeypatch.setattr(_orc, "_merllm_batch_available", lambda: True)
    monkeypatch.setattr(_orc, "_ensure_batch_poll_thread", lambda: None)
    monkeypatch.setattr(_orc, "_submit_reanalyze_batch", fake_submit_batch)

    _orc.run_reanalyze()

    assert "real_1"   in submitted
    assert "manual_1" not in submitted
```

(Note: `_submit_reanalyze_batch` is the helper the current loop calls; if the actual symbol name differs in the file, the test's monkeypatch will fail fast and surface the real name — adapt in Step 3 accordingly by grepping `def _submit` in `api/orchestrator.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_orchestrator.py::test_run_reanalyze_skips_manual_items -v`
Expected: FAIL — manual item currently enters the submit loop. (If the monkeypatch target name is wrong, fix it by matching the actual batch-submit helper in `run_reanalyze`; the assertion is what matters.)

- [ ] **Step 3: Add the guard in `run_reanalyze`**

In `api/orchestrator.py`, immediately after `all_records = db.get_all_items()` (~line 514), add:

```python
            # Manual cards have no source to re-extract from (issue #85).
            all_records = [r for r in all_records if r.get("source") != "manual"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_orchestrator.py::test_run_reanalyze_skips_manual_items -v`
Expected: PASS.

Run: `pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add api/orchestrator.py tests/test_orchestrator.py
git commit -m "fix(orchestrator): skip source='manual' items during reanalyze (#85)"
```

---

## Task 4: Widen `PATCH /analyses/{item_id}` to accept rich editable fields

**Files:**
- Modify: `api/app.py` (`patch_analysis` / update-item endpoint, ~line 918-1049)
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py`:

```python
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
```

(Note: the DB column for urgency_reason is named `urgency` per the items schema — the PATCH accepts `urgency_reason` as the public field name and writes to `urgency`. This keeps the wire API aligned with the JS card which reads `item.urgency_reason`.)

Actually — re-check the schema: the column is `urgency` (db.py:268). The JS at line 4079 reads `item.urgency_reason`. Those are inconsistent today — JS reads a field that the DB doesn't expose under that name. Grep confirms: `urgency` is the column, and `agent.py` writes it as `urgency`. The JS reading `item.urgency_reason` is probably dead / never populates. **Decision:** accept both `urgency_reason` and `urgency` as incoming keys; write to column `urgency`; and in Task 6 (UI), fix the JS to read `item.urgency` instead. The test above reflects this mapping.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_app.py::TestPatchAnalysisRichFields -v`
Expected: FAIL — all payloads other than priority/category/task_type/project_tag/is_passdown currently return 400 or are silently dropped.

- [ ] **Step 3: Widen the handler**

In `api/app.py`, in the `patch_analysis` / update-item endpoint around lines 948-978, replace the field-collection block with:

```python
    updates = {}
    if "priority" in body and body["priority"] in allowed_priorities:
        updates["priority"] = body["priority"]
    priority_reason = body.get("priority_reason")
    if priority_reason not in allowed_priority_reasons:
        priority_reason = None
    if "category" in body and body["category"] in allowed_categories:
        updates["category"] = body["category"]
        if body["category"] in ("noise", "fyi"):
            updates["has_action"] = 0
    if "task_type" in body and body["task_type"] in allowed_task_types:
        updates["task_type"] = body["task_type"]
    if "project_tag" in body:
        val = body["project_tag"]
        updates["project_tag"] = db.serialize_project_tags(val) if val else None
    if "is_passdown" in body and isinstance(body["is_passdown"], bool):
        updates["is_passdown"] = 1 if body["is_passdown"] else 0

    # Issue #85: content-level rich fields. These are writable on both
    # manual and generated items and are preserved across reanalyze via
    # user_edited_fields.
    if "title" in body and isinstance(body["title"], str):
        updates["title"] = body["title"][:500]
    if "summary" in body and isinstance(body["summary"], str):
        updates["summary"] = body["summary"]
    if "user_summary" in body and isinstance(body["user_summary"], str):
        updates["user_summary"] = body["user_summary"]
    # Accept either urgency_reason (wire name) or urgency (column name).
    _urgency_in = body.get("urgency_reason", body.get("urgency"))
    if isinstance(_urgency_in, str):
        updates["urgency"] = _urgency_in
    if "body_preview" in body and isinstance(body["body_preview"], str):
        updates["body_preview"] = body["body_preview"]
    if "hierarchy" in body and body["hierarchy"] in ("user", "project", "topic", "general"):
        updates["hierarchy"] = body["hierarchy"]
    if "goals" in body and isinstance(body["goals"], list):
        updates["goals"] = json.dumps(body["goals"])
    if "key_dates" in body and isinstance(body["key_dates"], list):
        updates["key_dates"] = json.dumps(body["key_dates"])

    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update.")
    # Track which fields the user has manually edited so reanalyze preserves them.
    _editable_fields = {
        "priority", "category", "project_tag", "is_passdown",
        # Issue #85 additions:
        "title", "summary", "user_summary", "urgency", "body_preview",
        "hierarchy", "goals", "key_dates",
    }
```

Leave the rest of the function (transactional update, keyword learning, embedding relearn, priority-override recording) untouched — those branches already gate on `"category" in updates` / `"priority" in updates` / `"project_tag" in updates` and are unaffected by the new fields.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_app.py::TestPatchAnalysisRichFields -v`
Expected: PASS (6 tests).

Run: `pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add api/app.py tests/test_app.py
git commit -m "feat(api): PATCH /analyses accepts title, summary, body_preview, goals, key_dates, urgency, hierarchy (#85)"
```

---

## Task 5: UI — open detail panel for manual todos (verify click-through works post-migration)

**Files:**
- Modify: `web/page/index.html` (`openDetail`, ~line 4045-4175; `openTodoDetail`, ~line 4025-4036)

This task is a UI verification pass — the migration plus Task 2 guarantees every todo now has `item_id`, so `openTodoDetail(item_id)` already works. This step hardens the display for `source='manual'` items (no From/To/CC; no source link; body treated as notes).

- [ ] **Step 1: Branch `openDetail` rendering on `source='manual'`**

In `web/page/index.html`, inside `openDetail(item)` (~line 4045):

Find the `meta` block (~line 4065-4071):

```javascript
  const meta = [
    item.author   ? detailRow('From', esc(item.author))   : '',
    item.to_field ? detailRow('To',   esc(item.to_field)) : '',
    item.cc_field ? detailRow('CC',   esc(item.cc_field)) : '',
    item.timestamp  ? detailRow('Time', new Date(item.timestamp).toLocaleString()) : '',
    item.replied_at ? detailRow('Replied', new Date(item.replied_at).toLocaleString()) : '',
  ].filter(Boolean).join('');
```

Replace with:

```javascript
  const isManual = item.source === 'manual';
  const meta = [
    (!isManual && item.author)   ? detailRow('From', esc(item.author))   : '',
    (!isManual && item.to_field) ? detailRow('To',   esc(item.to_field)) : '',
    (!isManual && item.cc_field) ? detailRow('CC',   esc(item.cc_field)) : '',
    item.timestamp  ? detailRow('Created', new Date(item.timestamp).toLocaleString()) : '',
    (!isManual && item.replied_at) ? detailRow('Replied', new Date(item.replied_at).toLocaleString()) : '',
  ].filter(Boolean).join('');
```

Then find the footer rendering (~line 4169-4172):

```javascript
  const openLink = item.url
    ? `<a href="${esc(item.url)}" target="_blank" class="connect-btn" style="text-decoration:none">↗ Open in ${item.source}</a>`
    : '<span style="font-size:11px;color:var(--muted)">No source link</span>';
  $('detailFoot').innerHTML = openLink;
```

Replace with:

```javascript
  const openLink = isManual
    ? '<span style="font-size:11px;color:var(--muted)">Manual card</span>'
    : (item.url
        ? `<a href="${esc(item.url)}" target="_blank" class="connect-btn" style="text-decoration:none">↗ Open in ${item.source}</a>`
        : '<span style="font-size:11px;color:var(--muted)">No source link</span>');
  $('detailFoot').innerHTML = openLink;
```

- [ ] **Step 2: Fix the urgency_reason → urgency field-name mismatch**

Find (~line 4079):

```javascript
  const urgency = item.urgency_reason ? `
    <div class="detail-section">
      <div class="detail-label">Why This Priority</div>
      <div class="detail-value" style="color:var(--muted);font-size:11px">${esc(item.urgency_reason)}</div>
    </div>` : '';
```

Replace with:

```javascript
  const urgencyText = item.urgency || item.urgency_reason || '';
  const urgency = urgencyText ? `
    <div class="detail-section">
      <div class="detail-label">Why This Priority</div>
      <div class="detail-value" style="color:var(--muted);font-size:11px">${esc(urgencyText)}</div>
    </div>` : '';
```

- [ ] **Step 3: Smoke test in browser**

From repo root:

```bash
docker compose up -d --build
```

Open `http://localhost:<port>/` (whatever port docker-compose publishes; check `docker-compose.yml`), switch to the Todos tab, click a manual todo row → the right-side detail panel should open. Confirm:
- The panel shows the manual card’s title at the top.
- No “From/To/CC” rows.
- Footer reads “Manual card”, not a source link.
- Existing generated cards still render From/To/CC and the source link.

- [ ] **Step 4: Commit**

```bash
git add web/page/index.html
git commit -m "feat(ui): detail panel renders manual cards without From/To/CC/source-link (#85)"
```

---

## Task 6: UI — inline editor for body / notes (body_preview) and user_summary

**Files:**
- Modify: `web/page/index.html` (inside `openDetail`, the body-preview section ~line 4085-4089, plus the right-rail edit block ~line 4153-4167)

- [ ] **Step 1: Replace the read-only body rendering with an editable textarea**

Find (~line 4085):

```javascript
  const bodyText = item.body_preview ? `
    <div class="detail-section">
      <div class="detail-label">Content</div>
      <div class="detail-body-text">${esc(item.body_preview)}</div>
    </div>` : '';
```

Replace with:

```javascript
  // Issue #85: editable body. For manual cards, this is the primary notes
  // surface. For generated cards, it seeds from the email body_preview and
  // can be annotated; edits are preserved across reanalyze (user_edited_fields).
  const bodyLabel = isManual ? 'Notes' : 'Content';
  const bodyText = `
    <div class="detail-section">
      <div class="detail-label">${bodyLabel}</div>
      <textarea id="detailBody_body_preview"
                class="detail-body-editor"
                rows="6"
                data-item-id="${esc(item.item_id)}"
                placeholder="${isManual ? 'Write notes about this card…' : ''}"
                onblur="saveDetailField(this,'body_preview')"
      >${esc(item.body_preview || '')}</textarea>
    </div>`;
```

Add this CSS (find an existing `.detail-body-text` rule and add adjacent):

```css
.detail-body-editor {
  width: 100%;
  min-height: 100px;
  padding: 8px;
  background: var(--bg);
  border: 1px solid var(--border2);
  border-radius: var(--r);
  color: var(--text);
  font-size: 12px;
  font-family: inherit;
  resize: vertical;
}
.detail-body-editor:focus { border-color: var(--accent); outline: none; }
```

(Search for `.detail-body-text` in the `<style>` block near the top and add the new rule near it.)

- [ ] **Step 2: Add the `saveDetailField` helper**

Find the end of the existing `saveDetailEdits(...)` function (grep `function saveDetailEdits`) and add immediately after it:

```javascript
async function saveDetailField(el, fieldName) {
  const itemId = el.dataset.itemId || el.closest('[data-item-id]')?.dataset.itemId;
  if (!itemId) return;
  const value = el.value;
  try {
    await api(`/analyses/${encodeURIComponent(itemId)}`, 'PATCH', { [fieldName]: value });
    const analysis = findAnalysis(itemId);
    if (analysis) analysis[fieldName] = value;
  } catch (e) {
    console.warn(`saveDetailField(${fieldName}):`, e);
  }
}
```

- [ ] **Step 3: Smoke test**

Rebuild & refresh the browser. Open a manual card, type a note in the textarea, click elsewhere (blur) → network tab shows `PATCH /analyses/<id>` with `{body_preview: "…"}` → reload page → the note persists. Repeat on a generated card — note persists alongside the original email body.

- [ ] **Step 4: Commit**

```bash
git add web/page/index.html
git commit -m "feat(ui): editable body/notes textarea in card detail panel (#85)"
```

---

## Task 7: UI — inline editor for `goals` (add/remove/edit list)

**Files:**
- Modify: `web/page/index.html` (inside `openDetail`, goals section ~line 4122-4126, plus new helpers)

- [ ] **Step 1: Replace read-only goals with an editable list**

Find (~line 4122):

```javascript
  const goals = (item.goals || []).length ? `
    <div class="detail-section">
      <div class="detail-label">Goals</div>
      <ul class="detail-list">${(item.goals || []).map(g => `<li>${esc(g)}</li>`).join('')}</ul>
    </div>` : '';
```

Replace with:

```javascript
  const goalsList = Array.isArray(item.goals) ? item.goals : [];
  const goals = `
    <div class="detail-section" data-item-id="${esc(item.item_id)}">
      <div class="detail-label">Goals</div>
      <ul class="detail-goals-list" id="detailGoalsList">
        ${goalsList.map((g, i) => `
          <li class="detail-goal-row" data-idx="${i}">
            <input type="text" class="detail-goal-input" value="${esc(g)}"
                   onblur="saveGoalEdit('${esc(item.item_id)}', ${i}, this.value)">
            <button class="ia" onclick="removeGoal('${esc(item.item_id)}', ${i})" title="Remove">✕</button>
          </li>
        `).join('')}
      </ul>
      <button class="ia detail-add-btn" onclick="addGoal('${esc(item.item_id)}')">+ Add goal</button>
    </div>`;
```

CSS additions (near other detail-* rules):

```css
.detail-goals-list, .detail-dates-list { list-style: none; padding: 0; margin: 0 0 6px 0; }
.detail-goal-row, .detail-date-row { display: flex; gap: 4px; align-items: center; margin-bottom: 4px; }
.detail-goal-input, .detail-date-desc {
  flex: 1; padding: 3px 7px; background: var(--bg);
  border: 1px solid var(--border2); border-radius: var(--r);
  color: var(--text); font-size: 11px;
}
.detail-date-input { padding: 3px 7px; background: var(--bg); border: 1px solid var(--border2); border-radius: var(--r); color: var(--text); font-size: 11px; }
.detail-add-btn { font-size: 11px; padding: 3px 8px; color: var(--muted); }
.detail-add-btn:hover { color: var(--accent); }
```

- [ ] **Step 2: Add goal CRUD helpers**

Append to the JS (immediately after `saveDetailField`):

```javascript
async function _patchGoals(itemId, newGoals) {
  const analysis = findAnalysis(itemId);
  if (!analysis) return;
  try {
    await api(`/analyses/${encodeURIComponent(itemId)}`, 'PATCH', { goals: newGoals });
    analysis.goals = newGoals;
  } catch (e) {
    console.warn('_patchGoals:', e);
  }
}
async function addGoal(itemId) {
  const analysis = findAnalysis(itemId);
  if (!analysis) return;
  const goals = [...(analysis.goals || []), ''];
  await _patchGoals(itemId, goals);
  openDetail(analysis); // re-render
}
async function removeGoal(itemId, idx) {
  const analysis = findAnalysis(itemId);
  if (!analysis) return;
  const goals = [...(analysis.goals || [])];
  goals.splice(idx, 1);
  await _patchGoals(itemId, goals);
  openDetail(analysis);
}
async function saveGoalEdit(itemId, idx, value) {
  const analysis = findAnalysis(itemId);
  if (!analysis) return;
  const goals = [...(analysis.goals || [])];
  goals[idx] = value;
  await _patchGoals(itemId, goals);
}
```

- [ ] **Step 3: Smoke test**

Open a manual card → detail panel → click “+ Add goal” → a new blank input appears → type “draft proposal”, blur → reload page → goal persists. Remove it → it disappears and PATCH returns ok.

- [ ] **Step 4: Commit**

```bash
git add web/page/index.html
git commit -m "feat(ui): editable goals list on card detail panel (#85)"
```

---

## Task 8: UI — inline editor for `key_dates` (add/remove/edit entries)

**Files:**
- Modify: `web/page/index.html` (inside `openDetail`, key_dates section ~line 4128-4134, plus new helpers)

- [ ] **Step 1: Replace read-only key dates with an editable list**

Find (~line 4128):

```javascript
  const dates = (item.key_dates || []).length ? `
    <div class="detail-section">
      <div class="detail-label">Key Dates</div>
      <ul class="detail-list">${(item.key_dates || []).map(d =>
        `<li>${d.date ? `<strong>${esc(String(d.date))}</strong> — ` : ''}${esc(d.description || '')}</li>`
      ).join('')}</ul>
    </div>` : '';
```

Replace with:

```javascript
  const kdList = Array.isArray(item.key_dates) ? item.key_dates : [];
  const dates = `
    <div class="detail-section" data-item-id="${esc(item.item_id)}">
      <div class="detail-label">Key Dates</div>
      <ul class="detail-dates-list" id="detailDatesList">
        ${kdList.map((d, i) => `
          <li class="detail-date-row" data-idx="${i}">
            <input type="date" class="detail-date-input" value="${esc(d.date || '')}"
                   onblur="saveKeyDateEdit('${esc(item.item_id)}', ${i}, 'date', this.value)">
            <input type="text" class="detail-date-desc" value="${esc(d.description || '')}" placeholder="what's happening"
                   onblur="saveKeyDateEdit('${esc(item.item_id)}', ${i}, 'description', this.value)">
            <button class="ia" onclick="removeKeyDate('${esc(item.item_id)}', ${i})" title="Remove">✕</button>
          </li>
        `).join('')}
      </ul>
      <button class="ia detail-add-btn" onclick="addKeyDate('${esc(item.item_id)}')">+ Add date</button>
    </div>`;
```

- [ ] **Step 2: Add key-date CRUD helpers**

Append after the goal helpers:

```javascript
async function _patchKeyDates(itemId, newList) {
  const analysis = findAnalysis(itemId);
  if (!analysis) return;
  try {
    await api(`/analyses/${encodeURIComponent(itemId)}`, 'PATCH', { key_dates: newList });
    analysis.key_dates = newList;
  } catch (e) {
    console.warn('_patchKeyDates:', e);
  }
}
async function addKeyDate(itemId) {
  const analysis = findAnalysis(itemId);
  if (!analysis) return;
  const list = [...(analysis.key_dates || []), { date: '', description: '' }];
  await _patchKeyDates(itemId, list);
  openDetail(analysis);
}
async function removeKeyDate(itemId, idx) {
  const analysis = findAnalysis(itemId);
  if (!analysis) return;
  const list = [...(analysis.key_dates || [])];
  list.splice(idx, 1);
  await _patchKeyDates(itemId, list);
  openDetail(analysis);
}
async function saveKeyDateEdit(itemId, idx, field, value) {
  const analysis = findAnalysis(itemId);
  if (!analysis) return;
  const list = [...(analysis.key_dates || [])];
  list[idx] = { ...list[idx], [field]: value };
  await _patchKeyDates(itemId, list);
}
```

- [ ] **Step 3: Smoke test**

Open a card → detail panel → click “+ Add date” → blank date + description appear → pick a date, type “submit draft”, blur both → reload → entries persist.

- [ ] **Step 4: Commit**

```bash
git add web/page/index.html
git commit -m "feat(ui): editable key_dates list on card detail panel (#85)"
```

---

## Task 9: UI — inline editor for summary / urgency / title

**Files:**
- Modify: `web/page/index.html` (inside `openDetail`, summary block ~line 4073-4077, urgency block ~line 4079-4083, title ~line 4047)

- [ ] **Step 1: Replace read-only summary with a textarea editor**

Find:

```javascript
  const summary = item.summary ? `
    <div class="detail-section">
      <div class="detail-label">Summary</div>
      <div class="detail-value">${esc(item.summary)}</div>
    </div>` : '';
```

Replace with:

```javascript
  const summary = `
    <div class="detail-section">
      <div class="detail-label">Summary</div>
      <textarea class="detail-body-editor" rows="2"
                data-item-id="${esc(item.item_id)}"
                placeholder="one-line summary"
                onblur="saveDetailField(this,'summary')"
      >${esc(item.summary || '')}</textarea>
    </div>`;
```

- [ ] **Step 2: Replace read-only urgency with a text editor**

Find the urgency block from Task 5 Step 2 output:

```javascript
  const urgencyText = item.urgency || item.urgency_reason || '';
  const urgency = urgencyText ? `
    <div class="detail-section">
      <div class="detail-label">Why This Priority</div>
      <div class="detail-value" style="color:var(--muted);font-size:11px">${esc(urgencyText)}</div>
    </div>` : '';
```

Replace with:

```javascript
  const urgencyText = item.urgency || item.urgency_reason || '';
  const urgency = `
    <div class="detail-section">
      <div class="detail-label">Why This Priority</div>
      <textarea class="detail-body-editor" rows="2"
                data-item-id="${esc(item.item_id)}"
                placeholder="why this matters"
                onblur="saveDetailField(this,'urgency')"
      >${esc(urgencyText)}</textarea>
    </div>`;
```

- [ ] **Step 3: Make title editable**

Find (~line 4047):

```javascript
  $('detailTitle').textContent = item.title || '(no title)';
```

Replace with:

```javascript
  const titleEl = $('detailTitle');
  titleEl.innerHTML = `
    <input type="text" class="detail-title-input"
           data-item-id="${esc(item.item_id)}"
           value="${esc(item.title || '')}"
           placeholder="(no title)"
           onblur="saveDetailField(this,'title')">`;
```

CSS (add near other detail-* rules):

```css
.detail-title-input {
  width: 100%; font-size: 14px; font-weight: 600;
  padding: 3px 6px; background: transparent;
  border: 1px solid transparent; border-radius: var(--r);
  color: var(--text);
}
.detail-title-input:hover { border-color: var(--border2); }
.detail-title-input:focus { border-color: var(--accent); outline: none; background: var(--bg); }
```

- [ ] **Step 4: Smoke test**

Edit title → blur → reload → persists. Same for summary, urgency.

- [ ] **Step 5: Commit**

```bash
git add web/page/index.html
git commit -m "feat(ui): editable title/summary/urgency on card detail panel (#85)"
```

---

## Task 10: UI — inline “Add task” and edit-in-place for linked todos

**Files:**
- Modify: `web/page/index.html` (inside `openDetail`, linked-todos section ~line 4092-4120, plus new helper)

- [ ] **Step 1: Add “+ Add task” button and inline edit controls**

Find the `actions` template (~line 4092-4120) and replace with:

```javascript
  const linkedTodos = allTodos.filter(t => t.item_id === item.item_id);
  const actions = `
    <div class="detail-section" id="detailTodosSection" data-item-id="${esc(item.item_id)}">
      <div class="detail-label">Tasks</div>
      ${linkedTodos.map(t => {
        const st = t.status || (t.done ? 'done' : 'open');
        const cbClick = st !== 'open'
          ? `detailToggleDone(${t.doc_id},false,'${esc(item.item_id)}')`
          : `detailToggleDone(${t.doc_id},true,'${esc(item.item_id)}')`;
        const cbContent = st === 'done' ? '✓' : st === 'assigned' ? '⇒' : '';
        const cbStyle   = st === 'assigned' ? 'style="background:rgba(64,184,255,.12);border-color:var(--blue);color:var(--blue)"' : '';
        const assignedInfo = st === 'assigned' && t.assigned_to
          ? `<span style="color:var(--blue);font-size:10px;margin-left:4px">⇒ ${esc(t.assigned_to)}</span>` : '';
        const assignBtn = st !== 'done'
          ? `<button class="ia" onclick="showAssignPicker(${t.doc_id},'${esc(item.item_id)}',this)" title="Assign">⇒</button>` : '';
        const dlVal = t.deadline || '';
        return `
        <div class="detail-todo-row" data-doc-id="${t.doc_id}">
          <button class="cb" ${cbStyle} onclick="${cbClick}">${cbContent}</button>
          <input type="text" class="detail-todo-desc-input"
                 value="${esc(t.description)}"
                 data-doc-id="${t.doc_id}"
                 onblur="saveLinkedTodoField(this,'description')">
          <input type="date" class="detail-todo-dl-input" value="${dlVal}"
                 data-doc-id="${t.doc_id}"
                 onblur="saveLinkedTodoField(this,'deadline')">
          ${assignedInfo}
          ${assignBtn}
          <button class="ia" onclick="deleteLinkedTodo(${t.doc_id},'${esc(item.item_id)}')" title="Remove task">✕</button>
        </div>`;
      }).join('')}
      <button class="ia detail-add-btn" onclick="addLinkedTodo('${esc(item.item_id)}')">+ Add task</button>
    </div>`;
```

CSS additions:

```css
.detail-todo-row { display: flex; gap: 4px; align-items: center; margin-bottom: 4px; flex-wrap: wrap; }
.detail-todo-desc-input {
  flex: 2; min-width: 120px; padding: 3px 7px; background: var(--bg);
  border: 1px solid var(--border2); border-radius: var(--r);
  color: var(--text); font-size: 11px;
}
.detail-todo-dl-input {
  padding: 3px 7px; background: var(--bg); border: 1px solid var(--border2);
  border-radius: var(--r); color: var(--text); font-size: 11px;
}
```

- [ ] **Step 2: Add linked-todo helpers**

Append after the key-date helpers:

```javascript
async function addLinkedTodo(itemId) {
  const resp = await api('/todos', 'POST', {
    description: 'new task',
    priority: 'medium',
    item_id: itemId,
  });
  // Refetch todos into the cache and re-render the panel.
  allTodos = await api('/todos');
  const analysis = findAnalysis(itemId);
  if (analysis) openDetail(analysis);
  loadStats?.();
}

async function saveLinkedTodoField(el, fieldName) {
  const docId = Number(el.dataset.docId);
  const value = fieldName === 'deadline' ? (el.value || null) : el.value;
  try {
    await api(`/todos/${docId}`, 'PATCH', { [fieldName]: value });
    const idx = allTodos.findIndex(t => t.doc_id === docId);
    if (idx >= 0) allTodos[idx] = { ...allTodos[idx], [fieldName]: value };
  } catch (e) {
    console.warn('saveLinkedTodoField:', e);
  }
}

async function deleteLinkedTodo(docId, itemId) {
  try {
    await api(`/todos/${docId}`, 'DELETE');
    allTodos = allTodos.filter(t => t.doc_id !== docId);
    const analysis = findAnalysis(itemId);
    if (analysis) openDetail(analysis);
    loadStats?.();
  } catch (e) {
    console.warn('deleteLinkedTodo:', e);
  }
}
```

- [ ] **Step 3: Smoke test**

Open a manual card → “+ Add task” → a new task appears with description “new task” → rename it, set a date, blur → reload → persists. On a generated card with existing tasks, rename an LLM-generated task → reload → rename persists. Remove a task → it disappears from both the card and the main Todos list.

- [ ] **Step 4: Commit**

```bash
git add web/page/index.html
git commit -m "feat(ui): inline add/edit/delete for linked tasks in card detail panel (#85)"
```

---

## Task 11: UI — replace minimal “Add action” inline form with “New card” flow

**Files:**
- Modify: `web/page/index.html` (`toggleAddAction` ~line 2502, `submitNewTodo` ~line 2515, and the related form HTML)

- [ ] **Step 1: Add a `createManualCard` helper that POSTs and opens detail**

Append near `submitNewTodo`:

```javascript
async function createManualCard() {
  try {
    const resp = await api('/todos', 'POST', {
      description: 'new card',
      priority:    'medium',
    });
    // Refresh caches, then open the detail panel for the new item.
    await Promise.all([loadTodos(), loadItems()]);
    const newItemId = resp.item_id || `manual_${resp.doc_id}`;
    openTodoDetail(newItemId);
    loadStats?.();
  } catch (e) {
    $('statusMsg').textContent = 'Failed to create card: ' + e.message;
  }
}
```

- [ ] **Step 2: Replace the toolbar button**

Grep for `toggleAddAction` in the HTML to find the button that opens the inline form (it'll be something like `<button onclick="toggleAddAction()">+ Add action</button>` or similar in the Todos-view toolbar). Replace its `onclick` with `createManualCard()` and update its label to `+ New card`. Leave the old `addActionForm` HTML in place for now (it'll just not get toggled on).

Example before (approximate — find in context):

```html
<button class="ia" onclick="toggleAddAction()" title="New manual action">+ Add action</button>
```

After:

```html
<button class="ia" onclick="createManualCard()" title="Create a new manual card">+ New card</button>
```

- [ ] **Step 3: Remove the now-unused `addActionForm` block and `toggleAddAction` / `submitNewTodo` functions**

Grep for the `<div id="addActionForm"` element and delete the entire form block. Then delete `toggleAddAction` (line 2502) and `submitNewTodo` (line 2515). `saveTodoEdit` and `showTodoEdit` (the edit-in-place row functions for the Todos list) remain useful and stay.

- [ ] **Step 4: Smoke test**

Click “+ New card” → POST fires → todos/analyses reload → detail panel opens on a fresh manual card with title “new card”, empty body, empty goals, empty tasks, empty key_dates. Edit the title to something real, add a task, add a goal, close panel → the new card is in the Todos list.

- [ ] **Step 5: Commit**

```bash
git add web/page/index.html
git commit -m "feat(ui): replace Add-action inline form with New-card flow opening detail panel (#85)"
```

---

## Task 12: Docs + final polish

**Files:**
- Modify: `README.md` (if it mentions manual-todo creation or card editing)
- Modify: `INSTRUCTIONS.md` (if it documents the UI flow)
- Modify: `web/page/index.html` — help-panel text if parsival has embedded UI help (grep for “help” / “? button”)

- [ ] **Step 1: Update README**

Grep `README.md` for any “Add action”, “manual todo”, “action item” wording. Replace with the new “New card” terminology and describe the unified editable surface:

```markdown
## Cards

Todos appear as **cards** in the right-hand detail panel. Every card — manual
or LLM-generated — exposes the same editable surface: priority, category,
project tag, title, summary, notes, goals, key dates, linked tasks, and
“why this priority”. Edits on generated cards are preserved across
reanalysis via `user_edited_fields`, so the LLM never silently overwrites
a manual correction.

Click **+ New card** in the Todos toolbar to create a manual card. It opens
straight into the detail panel for editing.
```

- [ ] **Step 2: Update INSTRUCTIONS.md**

Similar grep + update. Keep it concise.

- [ ] **Step 3: Update embedded help (if any)**

Grep `web/page/index.html` for the help/about content. Add a short paragraph under the Todos section describing the unified card surface.

- [ ] **Step 4: Full regression pass**

Run the full pytest suite:

```bash
pytest -q
```

Expected: all tests pass (460 baseline + the new #85 tests).

Run a container rebuild and sanity-check each flow end-to-end:

```bash
docker compose up -d --build
```

Manual QA checklist:
1. Click a generated card → detail panel opens, From/To/CC/source link all still render, body is editable, summary/urgency/title editable.
2. Click a manual card created before this feature (backfilled by migration) → detail panel opens, no From/To/CC, “Manual card” footer, all fields editable.
3. Click **+ New card** → blank manual card opens, editable, persists after reload.
4. Add a goal → persists. Remove a goal → persists.
5. Add a key date → persists. Remove one → persists.
6. Add a task inside a card → appears in both the card and the main Todos list. Delete it → gone from both.
7. Edit a task description inside a card → change shows in the main Todos list too.
8. Run a reanalyze → manual cards are not re-processed (check logs or inspect timestamps); user-edited fields on generated cards are preserved.

- [ ] **Step 5: Commit**

```bash
git add README.md INSTRUCTIONS.md web/page/index.html
git commit -m "docs: document unified editable card surface (#85)"
```

---

## Self-review notes

**Spec coverage (from issue #85 body):**
- Priority, owner, deadline, status, assigned_to → already editable pre-#85; still editable on generated cards via the Todos-list edit row; `owner` is not in scope for card-level editing (lives on todo row). **Gap:** if the user expected card-level owner editing, that surfaces in daily-use and can be a follow-up.
- Project tag, hierarchy, category → already editable (project_tag, category), hierarchy added in Task 4.
- Summary, urgency reason → Task 9.
- Free-form body / description → Task 6.
- Action items → delivered via linked-todos CRUD in Task 10. `items.action_items` remains the LLM snapshot (read-only, intentional).
- Key dates → Task 8.
- Goals → Task 7.
- Information items → not delivered this pass; small follow-up if the user wants it. (YAGNI for now.)
- Edits survive reanalyze → Task 4 (user_edited_fields).
- Applies to both manual and generated → yes (unified detail panel).

**Placeholder scan:** no TBDs, no "similar to Task N", every step has concrete code / commands / expected output.

**Type consistency:** `item_id` always `manual_<doc_id>` across migration + POST. PATCH wire field names (`goals`, `key_dates`, `body_preview`, `summary`, `title`, `urgency_reason`/`urgency`, `hierarchy`, `user_summary`) consistent between Task 4 backend and Tasks 6-9 frontend. `saveDetailField` / `_patchGoals` / `_patchKeyDates` / `saveLinkedTodoField` naming uniform.
