# Issue #83 — Body Cleaning + Owner-Extraction Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop parsival mis-assigning broadcast-email tasks to `owner="me"`. Give the LLM a cleaned body (quoted reply chain stripped, SafeLinks tracking URLs stripped) so the user's name is no longer smuggled into the prompt by artifacts the user didn't author. Restore the Assigned tab by making correct owner extraction flow through the existing `auto_status="assigned"` gate in `_save_analysis`.

**Architecture:**
- Add two pure helpers in `api/agent.py`: `_strip_quoted_reply_tail(body)` (reuses `signatures._QUOTE_MARKERS`) and `_strip_safelinks(body)`. Compose into `_clean_body_for_llm(body)`.
- Call the composer in `build_prompt()` (line 1013) and `analyze()` (line 1283) when formatting the LLM prompt. Do NOT change `body_preview` storage (line 1147) or the embedding-hint input (lines 977, 1248) — those consume the original body.
- Tighten the prompt's owner rules with three explicit negative examples (quoted-chain mention, @-mention of user as third party, SafeLinks footer).
- Delete the regex safety net `postprocess_action_items`/`_user_mentioned` — it was always a weaker check than the LLM with clean input, and it caused silent false negatives when the user's name appeared in noise.
- Thread-aware hints (PR #79) are orthogonal: the `thread_todos_hint` channel gives the LLM structured prior-message context independent of the quoted chain, so stripping the chain does not reduce the information the LLM has.

**Tech Stack:** Python 3, FastAPI, SQLite, pytest. Existing regex patterns in `api/signatures.py`; prompt template in `api/agent.py`.

**Out of scope (deliberately):**
- Second LLM verification pass. Deferred until we measure post-fix error rate.
- Aggressive mid-body `From:\nSent:\nTo:` detection. Conservative hard-marker cut only.
- Look-ahead todo source (`source='lookahead'`) integration with the Assigned tab — those are manual inserts and a separate design question.

---

## File Structure

**Modified:**
- `api/agent.py`
  - Add: `_SAFELINKS_RE`, `_strip_quoted_reply_tail`, `_strip_safelinks`, `_clean_body_for_llm` near line 840 (after `extract_emails` helpers, before `build_prompt`).
  - Modify: `build_prompt` (line 1008, `body=item.body` → `body=_clean_body_for_llm(item.body)`).
  - Modify: `analyze` (line 1283, same change).
  - Modify: `build_analysis_from_llm_json` (line 1089, remove the `postprocess_action_items` call). Keep `scope_info` parameter for the prompt hint.
  - Delete: `postprocess_action_items` (line 620) and its `_user_mentioned` closure. Leave `compute_recipient_scope` and `_recipient_scope_hint` — those still drive the prompt's scope-aware reasoning.
  - Tighten: PROMPT template lines 152–164 (Action item rules) to add three explicit negative-example bullets.

**Tests (new/updated):**
- `tests/test_agent.py`
  - Add a `TestBodyCleaning` test class with 7 tests: quoted-chain cut variants, SafeLinks stripping, composition, idempotence.
  - Add `test_build_prompt_uses_cleaned_body` asserting the prompt string does not contain a known quoted-chain marker when the raw body does.
  - Add `test_broadcast_email_rv17_fixture_owner_not_me` integration test with a real RV17-style fixture.
  - Delete `test_postprocess_action_items_*` (all tests of the removed safety net).
- `tests/test_app.py`
  - Add `test_save_analysis_assigned_tab_populates_for_broadcast` asserting `status='assigned'` and `assigned_to` non-empty for a fixture that simulates the LLM producing `owner="Anna Simonitis"`.
- `tests/test_orchestrator.py`
  - Add `test_apply_batch_result_uses_cleaned_body` — both ingest paths must route through the same cleaning.

---

## Task 1: Add body-cleaning helpers (pure functions, TDD)

**Files:**
- Modify: `api/agent.py` — add helpers after line ~840 (before `build_prompt` at line 909)
- Test: `tests/test_agent.py` — new `TestBodyCleaning` class at end of file

- [ ] **Step 1.1: Write failing tests for `_strip_quoted_reply_tail`**

Add to `tests/test_agent.py`:

```python
class TestBodyCleaning:
    """Body-cleaning helpers that run before LLM prompt formatting (issue #83)."""

    def test_strip_quoted_reply_tail_outlook_original_message(self):
        from agent import _strip_quoted_reply_tail
        body = (
            "Hi team, question about RV17.\n"
            "\n"
            "-----Original Message-----\n"
            "From: Someone Else <someone@example.com>\n"
            "Subject: old\n"
        )
        out = _strip_quoted_reply_tail(body)
        assert "Original Message" not in out
        assert out.strip() == "Hi team, question about RV17."

    def test_strip_quoted_reply_tail_on_date_wrote(self):
        from agent import _strip_quoted_reply_tail
        body = (
            "Did we get started on testing?\n"
            "\n"
            "On Fri, Apr 17, 2026 at 12:01 AM Someone <a@b.com> wrote:\n"
            "> Previous message content\n"
        )
        out = _strip_quoted_reply_tail(body)
        assert "wrote:" not in out
        assert "Did we get started on testing?" in out

    def test_strip_quoted_reply_tail_from_header_block(self):
        from agent import _strip_quoted_reply_tail
        body = (
            "Peter will cover while I'm out.\n"
            "\n"
            "From: Chris Ward <Chris.Ward@UniversalOrlando.com>\n"
            "Sent: Thursday, April 16, 2026 8:36 AM\n"
            "To: Pierce, Peter <Peter.Pierce@universalorlando.com>; Reid Hall <reid.hall@prismsystems.com>\n"
            "Subject: RV17 and next up\n"
            "\n"
            "Body of earlier message mentioning Reid Hall\n"
        )
        out = _strip_quoted_reply_tail(body)
        assert "Chris.Ward" not in out
        assert "reid.hall" not in out.lower()
        assert "Peter will cover while I'm out." in out

    def test_strip_quoted_reply_tail_no_markers_returns_unchanged(self):
        from agent import _strip_quoted_reply_tail
        body = "Short message with no reply chain."
        assert _strip_quoted_reply_tail(body) == body

    def test_strip_quoted_reply_tail_handles_empty_string(self):
        from agent import _strip_quoted_reply_tail
        assert _strip_quoted_reply_tail("") == ""

    def test_strip_safelinks_preserves_visible_text_drops_tracking_url(self):
        from agent import _strip_safelinks
        body = (
            "Please see https://nam02.safelinks.protection.outlook.com/"
            "?url=https%3A%2F%2Fexample.com%2Fdoc&data=05%7C01%7Creid.hall"
            "%40prismsystems.com%7C...\n"
            "Thanks."
        )
        out = _strip_safelinks(body)
        assert "reid.hall" not in out.lower()
        assert "safelinks.protection.outlook.com" not in out
        assert "Thanks." in out

    def test_clean_body_for_llm_composes_both(self):
        from agent import _clean_body_for_llm
        body = (
            "Current message body.\n"
            "See https://nam02.safelinks.protection.outlook.com/?url=x&data=reid.hall%40prismsystems.com\n"
            "\n"
            "-----Original Message-----\n"
            "From: Other <other@example.com>\n"
            "Reid Hall was quoted here somewhere.\n"
        )
        out = _clean_body_for_llm(body)
        assert "Current message body." in out
        assert "Original Message" not in out
        assert "safelinks" not in out.lower()
        assert "reid.hall" not in out.lower()
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `cd /home/lobulus/GitHub/hexcaliper-parsival && PYTHONPATH=api pytest tests/test_agent.py::TestBodyCleaning -v`
Expected: 7 failures with `ImportError: cannot import name '_strip_quoted_reply_tail' from 'agent'`

- [ ] **Step 1.3: Implement helpers in `api/agent.py`**

Insert after line 840 (before `def build_prompt`). If there is a nearby `# ── Helpers ──` section divider, place the block right before `build_prompt`. Import `signatures` lazily to avoid a circular import (signatures imports `db`, not `agent`, so top-level is fine too — test both):

```python
# ── Body cleaning for LLM prompt (issue #83) ──────────────────────────────────
#
# The raw Outlook body often contains a quoted reply chain and SafeLinks
# tracking URLs decorated with the user's email as a parameter.  Both artifacts
# smuggle the user's name/email into the prompt in positions that are NOT
# directives to the user, which made the LLM assign owner="me" for tasks
# actually aimed at other recipients (issue #83).  These helpers produce a
# cleaned "current message only" body for LLM input; the original body is
# still stored in items.body_preview and fed to the embedding hint.

#: SafeLinks URLs — Outlook rewrites every external link so the URL contains
#: the user's email under &data=... .  We drop the whole URL; if the author
#: wrote a plain-text link adjacent, it is preserved.
_SAFELINKS_RE = re.compile(
    r"https://[A-Za-z0-9.-]*safelinks\.protection\.outlook\.com/\S*",
    re.IGNORECASE,
)


def _strip_quoted_reply_tail(body: str) -> str:
    """
    Truncate ``body`` at the first quoted-reply marker line.

    Reuses :data:`signatures._QUOTE_MARKERS` so the set of markers stays in
    one place.  Conservative: only hard boundaries (``-----Original
    Message-----``, ``From:`` header start-of-line, ``On <date> ... wrote:``,
    ``> `` prefix) trigger the cut.  Inline paraphrases of earlier messages
    are kept.

    :param body: Raw message body.
    :return: Body truncated at the first quoted-reply marker, or the full
             body if none matches.
    """
    if not body:
        return body
    from signatures import _QUOTE_MARKERS  # local import — signatures is leaf
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        for marker in _QUOTE_MARKERS:
            if marker.match(line):
                return "\n".join(lines[:idx]).rstrip()
    return body


def _strip_safelinks(body: str) -> str:
    """
    Drop Outlook SafeLinks tracking URLs from ``body``.

    These URLs carry the user's email in the ``&data=`` parameter and trip
    naive "is the user named in this email" checks.  Matching is non-greedy
    at whitespace boundaries to leave adjacent text intact.
    """
    if not body:
        return body
    return _SAFELINKS_RE.sub("", body)


def _clean_body_for_llm(body: str) -> str:
    """
    Produce the cleaned body fed to the LLM for analysis.

    Composition: strip SafeLinks first (so URL fragments in the reply chain
    cannot survive the chain cut as mid-line remnants), then strip the
    quoted reply tail.  Idempotent.
    """
    return _strip_quoted_reply_tail(_strip_safelinks(body))
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `cd /home/lobulus/GitHub/hexcaliper-parsival && PYTHONPATH=api pytest tests/test_agent.py::TestBodyCleaning -v`
Expected: 7 passes.

- [ ] **Step 1.5: Commit**

```bash
git add tests/test_agent.py api/agent.py
git commit -m "feat(ingest): add body-cleaning helpers for LLM prompt (refs #83)

Strip quoted reply chains and SafeLinks tracking URLs before passing the
body to the LLM analysis prompt.  Pure helpers only; wiring follows in a
separate commit.

Reuses signatures._QUOTE_MARKERS for the quoted-chain boundary set so the
two features stay in sync.  Helpers are idempotent and no-op on empty
input."
```

---

## Task 2: Wire cleaned body into build_prompt() and analyze()

**Files:**
- Modify: `api/agent.py` lines 1008–1033 (`build_prompt` PROMPT.format call)
- Modify: `api/agent.py` lines 1278–1303 (`analyze` PROMPT.format call)
- Test: `tests/test_agent.py` — new `test_build_prompt_uses_cleaned_body`

- [ ] **Step 2.1: Write failing test**

Add to `tests/test_agent.py`:

```python
def test_build_prompt_uses_cleaned_body():
    """The prompt the LLM sees must not contain the quoted reply chain or
    SafeLinks URLs — those are the artifacts that were tricking the model
    into owner='me' on broadcast emails (issue #83)."""
    from agent import build_prompt
    from models import RawItem

    body_with_noise = (
        "Question: are we on track for Monday?\n"
        "\n"
        "Click https://nam02.safelinks.protection.outlook.com/"
        "?url=x&data=reid.hall%40prismsystems.com for details.\n"
        "\n"
        "-----Original Message-----\n"
        "From: Someone Else <someone@example.com>\n"
        "Reid Hall was CC'd on the prior thread.\n"
    )
    item = RawItem(
        item_id="t1", source="outlook", direction="received",
        title="Status", author="Sender <s@x.com>",
        timestamp="2026-04-21T00:00:00", url=None,
        body=body_with_noise,
        metadata={"to": "reid.hall@prismsystems.com", "cc": ""},
    )

    prompt = build_prompt(item)

    # Body-derived fields that should be gone:
    assert "-----Original Message-----" not in prompt
    assert "safelinks.protection.outlook.com" not in prompt
    assert "Reid Hall was CC'd" not in prompt
    # The real current-message directive must still be there:
    assert "are we on track for Monday?" in prompt
```

- [ ] **Step 2.2: Run to verify it fails**

Run: `PYTHONPATH=api pytest tests/test_agent.py::test_build_prompt_uses_cleaned_body -v`
Expected: FAIL — the prompt still contains `-----Original Message-----` because `body=item.body` is passed raw.

- [ ] **Step 2.3: Update `build_prompt` to pass cleaned body**

In `api/agent.py` at line 1013, change:

```python
        body         = item.body,
```

to:

```python
        body         = _clean_body_for_llm(item.body),
```

- [ ] **Step 2.4: Update `analyze` to pass cleaned body**

In `api/agent.py` at line 1283, make the identical change:

```python
            body         = _clean_body_for_llm(item.body),
```

- [ ] **Step 2.5: Run targeted test, then full agent test suite**

Run: `PYTHONPATH=api pytest tests/test_agent.py::test_build_prompt_uses_cleaned_body -v`
Expected: PASS.

Then: `PYTHONPATH=api pytest tests/test_agent.py -v`
Expected: all prior agent tests still pass. If any test that constructed a fixture body with a `From:` line now fails, that fixture's assertion was implicitly relying on the quoted chain surviving — fix the fixture, not the helper.

- [ ] **Step 2.6: Commit**

```bash
git add tests/test_agent.py api/agent.py
git commit -m "feat(ingest): clean body before LLM prompt formatting (refs #83)

Route both build_prompt (batch path) and analyze (sync path) through
_clean_body_for_llm so the prompt never contains quoted reply chains or
SafeLinks tracking URLs.  items.body_preview and the embedding-hint input
still use the original body."
```

---

## Task 3: Tighten prompt owner-rules with negative examples

**Files:**
- Modify: `api/agent.py` lines 152–164 (PROMPT template — Action item rules section)
- Test: `tests/test_agent.py` — new `test_prompt_contains_negative_owner_examples`

- [ ] **Step 3.1: Write failing test**

```python
def test_prompt_contains_negative_owner_examples():
    """The PROMPT template must explicitly call out the three owner='me'
    traps we saw in real data (issue #83)."""
    from agent import PROMPT
    assert "quoted reply" in PROMPT.lower() or "reply chain" in PROMPT.lower()
    assert "@mention" in PROMPT.lower() or "@-mention" in PROMPT.lower()
    assert "third party" in PROMPT.lower() or "third-party" in PROMPT.lower()
```

- [ ] **Step 3.2: Run to verify it fails**

Run: `PYTHONPATH=api pytest tests/test_agent.py::test_prompt_contains_negative_owner_examples -v`
Expected: FAIL.

- [ ] **Step 3.3: Update PROMPT action-item rules**

In `api/agent.py`, locate the `Action item rules:` block (starts at line 152). Replace lines 160 onward (keeping lines 152–159 unchanged) with:

```
- If a directive has no identifiable owner (no name, no @mention, no direct address) and recipient scope is not "direct", OMIT the action_item rather than guessing.
- Do NOT assign owner="me" on the basis of the user's name appearing in any of these contexts — they are NOT directives to {user_name}:
  * A quoted reply chain or forwarded-message header below the current author's message (anything after "-----Original Message-----", "From: …", or "On <date>, <X> wrote:").
  * An @mention of {user_name} as a third-party to coordinate with (e.g. "…to help coordinate with @{user_name}'s team", "@{user_name}'s crew will handle X").
  * A tracking URL, disclaimer footer, or "Individualized for <email>" marker.
  These mentions place {user_name} in the text but do not address them.
- Past-tense reports of completed work ("we installed X", "the issue was resolved") are NOT action items — put them in information_items instead.
- Jira issues: has_action=true unless status is Done/Closed; owner="me" (these are pre-assigned).
- GitHub PR review requests: has_action=true, category=review, owner="me".
- Slack DMs: recipient scope is always "direct"; bias toward has_action=true.
```

- [ ] **Step 3.4: Run to verify it passes**

Run: `PYTHONPATH=api pytest tests/test_agent.py::test_prompt_contains_negative_owner_examples -v`
Expected: PASS.

- [ ] **Step 3.5: Commit**

```bash
git add api/agent.py tests/test_agent.py
git commit -m "feat(ingest): tighten prompt owner rules with negative examples (refs #83)

Explicitly enumerate the three contexts where the user's name appears but
is NOT a directive to them: quoted reply chains, third-party @mentions,
and tracking/disclaimer footers.  Paired with body cleaning in the prior
commit — the prompt tells the LLM the new contract."
```

---

## Task 4: Remove the regex safety net

**Files:**
- Modify: `api/agent.py` — delete `postprocess_action_items` (line 620) and its call site at line 1089
- Modify: `tests/test_agent.py` — delete tests covering the removed function

- [ ] **Step 4.1: Find and list existing safety-net tests**

Run: `PYTHONPATH=api pytest tests/test_agent.py -v -k postprocess 2>&1 | grep -E '(PASS|FAIL|test_)'`
Expected output: list of `test_postprocess_*` tests.

- [ ] **Step 4.2: Delete the safety-net call site**

In `api/agent.py` `build_analysis_from_llm_json`, remove lines 1085–1092 (the `postprocess_action_items` call and its comment block):

```python
    # Recipient-scope safety net: in group/broadcast emails where the user
    # is not named in the body, strip owner="me" false positives.  Items
    # assigned to OTHER named people are always preserved so delegated-work
    # tracking continues to work.
    action_items = postprocess_action_items(
        action_items, scope_info, item.body,
        config.USER_NAME or "", config.USER_EMAIL or "",
    )
```

Replace with a short comment explaining the design change:

```python
    # No regex safety net here by design (issue #83): the LLM is given a
    # cleaned body (quoted chain + SafeLinks stripped) and explicit
    # negative-example rules in the prompt.  Relying on string-match heuristics
    # over a noisy body was the failure mode we just fixed.
```

- [ ] **Step 4.3: Delete the `postprocess_action_items` function**

In `api/agent.py`, delete lines 620–678 (the full `def postprocess_action_items` body including `_user_mentioned` closure).

- [ ] **Step 4.4: Delete tests of the removed function**

In `tests/test_agent.py`, delete every test whose name starts with `test_postprocess_` (the list from Step 4.1). These tested the removed safety net and are no longer meaningful.

- [ ] **Step 4.5: Run full agent test suite**

Run: `PYTHONPATH=api pytest tests/test_agent.py -v`
Expected: all remaining tests pass; no reference to `postprocess_action_items` anywhere.

Sanity grep: `grep -n postprocess_action_items api/agent.py tests/test_agent.py`
Expected: no output.

- [ ] **Step 4.6: Commit**

```bash
git add api/agent.py tests/test_agent.py
git commit -m "refactor(ingest): remove regex owner=me safety net (closes #83)

The safety net bailed out whenever the user's name appeared anywhere in
the raw body — including quoted reply chains, @-mentions of the user as a
third party, and SafeLinks tracking URLs decorated with the user's email.
Those three contexts were the leak described in #83.

Replaced by: (1) cleaned body fed to the LLM (prior commits), (2) explicit
negative-example rules in the prompt, (3) scope hint that tells the LLM how
broadly the message is addressed.  The LLM with clean input is more
accurate than the regex was."
```

---

## Task 5: Integration test — Assigned tab populates end-to-end

**Files:**
- Test: `tests/test_app.py` — new `test_save_analysis_assigned_status_for_delegated_owner`

- [ ] **Step 5.1: Write the failing test**

This test does NOT depend on the LLM. It constructs an `Analysis` directly with `action_items=[ActionItem(owner="Anna Simonitis", ...)]` and calls `_save_analysis` to prove the app-layer gate at `api/app.py:655-665` turns a non-me owner into `status='assigned'`. That is the path the fix is meant to restore.

Add to `tests/test_app.py`:

```python
def test_save_analysis_assigned_status_for_delegated_owner(tmp_path, monkeypatch):
    """Regression guard for issue #83's Assigned-tab symptom: when the LLM
    produces a non-me owner, _save_analysis must set status='assigned' and
    populate assigned_to so the todo shows up in the Assigned tab."""
    import importlib, sys
    monkeypatch.setenv("PARSIVAL_DATA_DIR", str(tmp_path))
    # Force a fresh DB in the tmp dir
    for mod in ("db", "app", "orchestrator"):
        sys.modules.pop(mod, None)
    import db as _db
    _db.init_schema()

    from app import _save_analysis
    from models import Analysis, ActionItem

    a = Analysis(
        item_id="test-rv17",
        source="outlook",
        title="RV17 and next up",
        author="Chris Ward <Chris.Ward@UniversalOrlando.com>",
        timestamp="2026-04-16T12:36:00",
        url=None,
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
        conversation_id="conv-1",
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

    rows = _db.get_todos_for_item("test-rv17")
    assert len(rows) == 1, rows
    t = rows[0]
    assert t["owner"] == "Anna Simonitis"
    assert t["status"] == "assigned", t
    # resolve_owner_email should match "Anna Simonitis" against the To header
    assert t["assigned_to"] == "anna.simonitis@universalorlando.com"
```

- [ ] **Step 5.2: Run to verify it passes on the current code**

Run: `PYTHONPATH=api pytest tests/test_app.py::test_save_analysis_assigned_status_for_delegated_owner -v`
Expected: PASS. (The Assigned-tab gate at `_save_analysis` has always been correct — the bug was upstream. This test locks that path down so a future refactor can't silently break it.)

If this test FAILS, stop and investigate before continuing: it means something in `_save_analysis` has drifted from the expected behavior and the fix is incomplete.

- [ ] **Step 5.3: Commit**

```bash
git add tests/test_app.py
git commit -m "test(app): cover Assigned-tab delegated-owner path (refs #83)

Lock down the _save_analysis gate that turns a non-me owner into
status='assigned' and resolves assigned_to from To/CC headers.  The gate
was always correct; its upstream input (the LLM's owner field) was the
bug this issue fixes.  This test guards against future regressions on
the gate itself."
```

---

## Task 6: End-to-end batch-path test

**Files:**
- Test: `tests/test_orchestrator.py` — new `test_apply_batch_result_uses_cleaned_body`

- [ ] **Step 6.1: Write failing test**

Add to `tests/test_orchestrator.py`:

```python
def test_apply_batch_result_uses_cleaned_body(tmp_path, monkeypatch):
    """The batch-poll path (merLLM result → _apply_batch_result →
    build_analysis_from_llm_json) must apply the same body cleaning as
    the sync path, since both call build_analysis_from_llm_json which
    no longer runs postprocess_action_items (issue #83)."""
    # Body cleaning is upstream of build_analysis_from_llm_json (it lives in
    # build_prompt / analyze), so the batch path's cleanliness comes from
    # the prompt that was submitted at batch-submit time.  What this test
    # verifies: given an LLM response with owner="Logan Souza" for a broadcast
    # email fixture, the batch apply path stores a todo with that owner —
    # NOT silently rewritten to "me" by any surviving safety net.
    import importlib, sys, json
    monkeypatch.setenv("PARSIVAL_DATA_DIR", str(tmp_path))
    for mod in ("db", "app", "orchestrator"):
        sys.modules.pop(mod, None)
    import db as _db
    _db.init_schema()
    import app  # initializes orchestrator
    import orchestrator

    # Seed an item record that _raw_item_from_record can hydrate
    _db.upsert_item({
        "item_id": "batch-rv17",
        "source": "outlook",
        "direction": "received",
        "title": "RE: 4/15/2026",
        "author": "Alex Washington <alex@dubscontrols.com>",
        "timestamp": "2026-04-17T08:42:35",
        "url": None,
        "body_preview": "Logan, did we get started on CV/LiDAR testing?",
        "to_field": "Logan Souza <Logan.Souza@universalorlando.com>; Reid Hall <reid.hall@prismsystems.com>",
        "cc_field": "",
        "has_action": 0, "priority": "medium", "category": "fyi",
        "task_type": None, "summary": "", "urgency": None,
        "action_items": "[]", "hierarchy": "general", "is_passdown": 0,
        "project_tag": None, "conversation_id": "conv-2",
        "conversation_topic": None, "goals": "[]", "key_dates": "[]",
        "information_items": "[]", "is_replied": 0, "replied_at": None,
        "references": "[]", "situation_id": None,
    })

    llm_response = json.dumps({
        "category": "task",
        "priority": "medium",
        "has_action": True,
        "summary": "Follow up on CV/LiDAR testing",
        "urgency_reason": None,
        "hierarchy": "project",
        "action_items": [
            {"description": "Follow up on whether CV/LiDAR testing started",
             "deadline": None, "owner": "Logan Souza"}
        ],
        "information_items": [],
        "goals": [], "key_dates": [],
    })

    rec = _db.get_item("batch-rv17")
    orchestrator._apply_batch_result(rec, llm_response)

    rows = _db.get_todos_for_item("batch-rv17")
    assert len(rows) == 1, rows
    t = rows[0]
    assert t["owner"] == "Logan Souza"
    assert t["status"] == "assigned"
    assert t["assigned_to"] == "logan.souza@universalorlando.com"
```

- [ ] **Step 6.2: Run to verify it passes on the fixed code**

Run: `PYTHONPATH=api pytest tests/test_orchestrator.py::test_apply_batch_result_uses_cleaned_body -v`
Expected: PASS (after Tasks 1–4 are in place, the batch path no longer rewrites owner to "me").

- [ ] **Step 6.3: Run full test suite**

Run: `PYTHONPATH=api pytest tests/ -v`
Expected: all green.

- [ ] **Step 6.4: Commit**

```bash
git add tests/test_orchestrator.py
git commit -m "test(orchestrator): cover batch-path delegated-owner (refs #83)

Guards against a future regression where the batch path diverges from
the sync path on owner handling.  Both now funnel through
build_analysis_from_llm_json without a regex safety net."
```

---

## Task 7: Manual verification against real DB

**Files:**
- No code changes. Verification against user's live `data/parsival.db`.

- [ ] **Step 7.1: Record pre-fix counts**

Run:
```bash
python3 <<'EOF'
import sqlite3
c = sqlite3.connect('data/parsival.db').cursor()
c.execute("SELECT COUNT(*) FROM todos WHERE status='assigned' AND done=0")
print("assigned open:", c.fetchone()[0])
c.execute("SELECT COUNT(*) FROM todos WHERE owner='me' AND done=0 AND created_at > '2026-04-14'")
print("owner=me open since 2026-04-14:", c.fetchone()[0])
EOF
```

Record both numbers.

- [ ] **Step 7.2: Trigger re-analyze of the three example emails**

Use the parsival UI's re-analyze action OR call the reanalyze endpoint directly for each of the three item_ids from issue #83:
- RV17 thread (`title='RV17 and next up'` and `title='Re: RV17 and next up'`)
- CV/LiDAR (`title='RE: 4/15/2026'`)

Run (substitute the three item_ids):
```bash
curl -X POST http://localhost:PORT/api/items/<item_id>/reanalyze
```

- [ ] **Step 7.3: Verify expected outcomes**

Run:
```bash
python3 <<'EOF'
import sqlite3
c = sqlite3.connect('data/parsival.db').cursor()
for title in ("RV17 and next up", "Re: RV17 and next up", "RE: 4/15/2026"):
    c.execute("""
      SELECT owner, status, assigned_to, substr(description,1,60)
      FROM todos WHERE title = ? AND done = 0
    """, (title,))
    rows = c.fetchall()
    print(f"\n{title}: {len(rows)} open todo(s)")
    for r in rows:
        print(f"  owner={r[0]!r} status={r[1]!r} assigned_to={r[2]!r}")
        print(f"    {r[3]!r}")

c.execute("SELECT COUNT(*) FROM todos WHERE status='assigned' AND done=0")
print("\nassigned open (post-fix):", c.fetchone()[0])
EOF
```

Acceptance (from issue #83):
- RV17 threads must produce at least one todo with `owner` resolving to `Anna Simonitis`, `Logan Souza`, or similar non-"me" recipient, and `status='assigned'`.
- CV/LiDAR must produce todos directed at Logan Souza with `owner='Logan Souza'` (or resolved email) and `status='assigned'`.
- "assigned open (post-fix)" count must be > 0.

- [ ] **Step 7.4: Vergos baseline regression check**

Run:
```bash
python3 <<'EOF'
import sqlite3
c = sqlite3.connect('data/parsival.db').cursor()
c.execute("""
  SELECT item_id FROM items
  WHERE author LIKE '%Vergos%' OR to_field LIKE '%Vergos%'
  LIMIT 1
""")
row = c.fetchone()
print("Vergos item_id for reanalyze:", row)
EOF
```

Re-analyze that item via the UI and confirm: a `Vergos, George (UDX)` todo still emerges with `status='assigned'` and `assigned_to='george.vergos@...'`. If not, the fix broke a previously-working path — roll back Task 4 and investigate before proceeding.

- [ ] **Step 7.5: Open Assigned tab in the UI**

Visit the parsival web UI, click the Assigned vtab, confirm the badge shows a positive count and the tab lists the expected delegated tasks (Anna Simonitis, Logan Souza, Vergos, etc.).

- [ ] **Step 7.6: Close issue #83 with evidence**

Run (substitute the merge commit hash from the actual PR):
```bash
gh issue close 83 --repo rcanterberryhall/hexcaliper-parsival \
  --comment "Resolved in commit <hash> — body cleaning (quoted chain + SafeLinks) + prompt negative examples + removed regex safety net.  Assigned tab count went from 0 to N after re-analyze of recent Outlook items.  RV17/CV-LiDAR fixtures now produce status='assigned' with correct assigned_to.  Vergos baseline regression-checked."
```

---

## Self-Review Checklist

**Spec coverage (against issue #83):**
- [x] `compute_recipient_scope` and `_recipient_scope_hint` preserved — Task 4 explicitly keeps them.
- [x] Quoted-chain stripping — Task 1.
- [x] SafeLinks stripping — Task 1.
- [x] Prompt negative examples — Task 3.
- [x] Regex safety net removed — Task 4.
- [x] Sync/batch body-length mismatch resolved — both paths now feed `_clean_body_for_llm(item.body)` to `PROMPT.format`, which is then unbounded at prompt-construction time (no `[:2000]` truncation on the cleaned body path). `body_preview` truncation still applies to storage, which is UI-only.
- [x] Thread awareness (PR #79) unaffected — Task 1/2 do not touch `thread_todos_hint`; verified by leaving `_render_thread_todos_hint` and the `thread_todos` parameter untouched.
- [x] Assigned-tab acceptance — Tasks 5, 6, 7.3.

**Placeholder scan:** none; every code step contains the full code block. Every test step contains the full test. Every commit step contains the full message.

**Type consistency:**
- `_clean_body_for_llm`, `_strip_quoted_reply_tail`, `_strip_safelinks` all take `body: str` → `str`, called consistently.
- `postprocess_action_items` removal is clean: the signature changed nothing upstream because it was only called from `build_analysis_from_llm_json`. Confirmed by grep in Step 4.5.
- No test introduces a type not defined in an earlier task.
