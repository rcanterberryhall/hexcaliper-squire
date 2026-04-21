import json
from unittest.mock import MagicMock, patch

import agent
from models import RawItem


def _raw(source="github", item_id="1", title="Test", body="content"):
    return RawItem(
        source=source, item_id=item_id, title=title, body=body,
        url="http://x", author="alice", timestamp="2024-01-01T00:00:00",
    )


def _mock_response(data: dict) -> MagicMock:
    m = MagicMock()
    m.json.return_value = {"response": json.dumps(data)}
    m.raise_for_status.return_value = None
    return m


def test_analyze_parses_full_response():
    payload = {
        "has_action": True,
        "priority": "high",
        "category": "task",
        "action_items": [{"description": "Ship it", "deadline": "2024-03-01", "owner": "me"}],
        "summary": "Deploy the release",
        "urgency_reason": "overdue",
    }
    with patch("agent.llm.generate", return_value=json.dumps(payload)):
        result = agent.analyze(_raw())

    assert result.has_action is True
    assert result.priority == "high"
    assert result.category == "task"
    assert result.summary == "Deploy the release"
    assert len(result.action_items) == 1
    assert result.action_items[0].description == "Ship it"
    assert result.action_items[0].deadline == "2024-03-01"


def test_analyze_defaults_on_empty_response():
    with patch("agent.llm.generate", return_value="{}"):
        result = agent.analyze(_raw(title="Fallback title"))

    assert result.priority == "medium"
    assert result.category == "fyi"
    assert result.summary == "Fallback title"  # falls back to item.title
    assert result.action_items == []


def test_analyze_jira_fallback_creates_action_item():
    """Jira items always get an action item even if LLM returns nothing."""
    item = RawItem(
        source="jira", item_id="PROJ-42", title="Fix login bug",
        body="", url="", author="", timestamp="2024-01-01",
        metadata={"due": "2024-04-01"},
    )
    with patch("agent.llm.generate", return_value="{}"):
        result = agent.analyze(item)

    assert len(result.action_items) == 1
    assert "Fix login bug" in result.action_items[0].description
    assert result.action_items[0].deadline == "2024-04-01"


# ── build_prompt thread_todos hint (parsival#79) ───────────────────────────────

def test_build_prompt_omits_thread_todos_hint_when_empty():
    prompt_empty = agent.build_prompt(_raw(), thread_todos=[])
    prompt_none  = agent.build_prompt(_raw(), thread_todos=None)
    prompt_default = agent.build_prompt(_raw())

    for p in (prompt_empty, prompt_none, prompt_default):
        assert "Already tracked in this email thread" not in p


def test_build_prompt_includes_thread_todos_hint_when_present():
    todos_list = [
        {"description": "Sign the RV9 auth letter", "owner": "me", "deadline": "2026-04-20"},
        {"description": "Receive replacement transformer", "owner": "me", "deadline": None},
    ]
    prompt = agent.build_prompt(_raw(), thread_todos=todos_list)

    assert "Already tracked in this email thread" in prompt
    assert "do NOT re-emit" in prompt
    assert "still emit any genuinely new tasks" in prompt
    assert "Sign the RV9 auth letter" in prompt
    assert "Receive replacement transformer" in prompt
    assert "2026-04-20" in prompt


def test_analyze_passes_thread_todos_hint_to_llm():
    """Sync path (analyze) must render the thread_todos hint into the prompt,
    not just build_prompt (which is the batch path)."""
    captured = {}

    def fake_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        return "{}"

    todos_list = [{"description": "Book the training room",
                   "owner": "me", "deadline": None}]

    with patch("agent.llm.generate", side_effect=fake_generate):
        agent.analyze(_raw(), thread_todos=todos_list)

    assert "Already tracked in this email thread" in captured["prompt"]
    assert "Book the training room" in captured["prompt"]


def test_analyze_skips_action_items_without_description():
    payload = {
        "has_action": True,
        "priority": "low",
        "category": "fyi",
        "action_items": [{"description": "", "deadline": None, "owner": "me"}],
        "summary": "Nothing to do",
        "urgency_reason": None,
    }
    with patch("agent.llm.generate", return_value=json.dumps(payload)):
        result = agent.analyze(_raw())

    assert result.action_items == []


def test_analyze_batch_calls_progress_cb():
    items = [_raw(item_id=str(i)) for i in range(3)]
    calls = []

    with patch("agent.llm.generate", return_value="{}"):
        agent.analyze_batch(items, progress_cb=lambda i, t, s, title: calls.append(i))

    assert calls == [0, 1, 2]


def test_analyze_batch_skips_failed_items():
    items = [_raw(item_id="ok"), _raw(item_id="bad"), _raw(item_id="ok2")]

    with patch("agent.llm.generate", side_effect=Exception("network error")):
        results = agent.analyze_batch(items)

    # Errors are swallowed; no results but no crash
    assert results == []


# ── Recipient scope classification ───────────────────────────────────────────

class TestComputeRecipientScope:
    def test_no_recipients_is_direct(self):
        r = agent.compute_recipient_scope("user@co.com", "", "")
        assert r["scope"] == "direct"
        assert r["total"] == 0

    def test_single_to_user_is_direct(self):
        r = agent.compute_recipient_scope("user@co.com", "User <user@co.com>", "")
        assert r["scope"] == "direct"
        assert r["total"] == 1
        assert r["user_in_to"] is True

    def test_two_to_three_is_small(self):
        r = agent.compute_recipient_scope(
            "user@co.com",
            "User <user@co.com>, Bob <bob@co.com>, Carol <carol@co.com>",
            "",
        )
        assert r["scope"] == "small"
        assert r["total"] == 3

    def test_six_recipients_is_group(self):
        to = ", ".join(f"p{i} <p{i}@co.com>" for i in range(5)) + ", User <user@co.com>"
        r = agent.compute_recipient_scope("user@co.com", to, "")
        assert r["scope"] == "group"
        assert r["total"] == 6

    def test_twelve_recipients_is_broadcast(self):
        to = ", ".join(f"p{i} <p{i}@co.com>" for i in range(11)) + ", User <user@co.com>"
        r = agent.compute_recipient_scope("user@co.com", to, "")
        assert r["scope"] == "broadcast"
        assert r["total"] == 12

    def test_distribution_list_forces_broadcast(self):
        # Only 2 addresses, but one is a DL → broadcast
        r = agent.compute_recipient_scope(
            "user@co.com",
            "Eng Team <eng-team@co.com>, User <user@co.com>",
            "",
        )
        assert r["scope"] == "broadcast"
        assert "eng-team@co.com" in r["dls"]

    def test_dl_local_prefixes(self):
        for addr in ("all-hands@co.com", "dl-engineering@co.com", "everyone@co.com", "team@co.com"):
            assert agent._is_distribution_list(addr) is True, addr

    def test_dl_domain_patterns(self):
        for addr in ("maintainers@lists.kernel.org", "devs@groups.google.com"):
            assert agent._is_distribution_list(addr) is True, addr

    def test_personal_address_not_dl(self):
        for addr in ("alice@co.com", "bob.smith@acme.org", "user@co.com"):
            assert agent._is_distribution_list(addr) is False, addr

    def test_user_absent_from_visible_is_broadcast(self):
        # User received via BCC or mailing list — not in To/CC
        r = agent.compute_recipient_scope(
            "user@co.com",
            "Alice <alice@co.com>, Bob <bob@co.com>",
            "",
        )
        assert r["scope"] == "broadcast"
        assert r["user_in_to"] is False


# ── build_analysis_from_llm_json (shared helper) ──────────────────────────────

class TestBuildAnalysisFromLlmJson:
    """The shared helper used by both analyze() (sync path) and the
    orchestrator batch poll path.  These tests pin down behaviour the two
    paths must agree on, so they cannot drift again."""

    def _direct_scope(self):
        return {
            "scope": "direct", "to_count": 1, "cc_count": 0, "total": 1,
            "dls": [], "user_in_to": True, "user_in_cc": False,
        }

    def _broadcast_scope(self):
        return {
            "scope": "broadcast", "to_count": 0, "cc_count": 0, "total": 12,
            "dls": [], "user_in_to": False, "user_in_cc": False,
        }

    def test_full_payload_populates_every_field(self):
        item = RawItem(
            source="outlook", item_id="x1", title="Ship the release",
            body="please ship today", url="http://x", author="bob@co.com",
            timestamp="2026-04-10T10:00:00+00:00",
            metadata={"to": "user@co.com", "cc": "", "is_replied": False,
                      "hierarchy": "general", "direction": "received"},
        )
        payload = {
            "has_action": True, "priority": "high", "category": "task",
            "task_type": "review",
            "action_items": [{"description": "Ship it", "deadline": "2026-04-11", "owner": "me"}],
            "summary": "Ship the release",
            "urgency_reason": "deadline today",
            "hierarchy": "personal",
            "is_passdown": False,
            "goals": ["release"], "key_dates": [{"label": "ship", "date": "2026-04-11"}],
            "information_items": [{"fact": "v2.1 ready", "relevance": "release"}],
        }
        result = agent.build_analysis_from_llm_json(
            item, json.dumps(payload), scope_info=self._direct_scope(),
        )
        assert result.priority == "high"
        assert result.category == "task"
        assert result.task_type == "review"
        assert result.summary == "Ship the release"
        assert result.hierarchy == "personal"
        assert len(result.action_items) == 1
        assert result.action_items[0].description == "Ship it"
        assert result.goals == ["release"]
        assert len(result.information_items) == 1

    def test_empty_payload_uses_defaults(self):
        item = RawItem(
            source="outlook", item_id="x2", title="Fallback title",
            body="", url="", author="", timestamp="2026-04-10T10:00:00+00:00",
        )
        result = agent.build_analysis_from_llm_json(
            item, "{}", scope_info=self._direct_scope(),
        )
        assert result.priority == "medium"
        assert result.category == "fyi"
        assert result.summary == "Fallback title"
        assert result.action_items == []

    def test_malformed_json_uses_defaults(self):
        item = RawItem(
            source="outlook", item_id="x3", title="t", body="", url="",
            author="", timestamp="2026-04-10T10:00:00+00:00",
        )
        result = agent.build_analysis_from_llm_json(
            item, "{not json", scope_info=self._direct_scope(),
        )
        assert result.priority == "medium"
        assert result.category == "fyi"
        assert result.action_items == []

    def test_jira_fallback_creates_action_item(self):
        item = RawItem(
            source="jira", item_id="PROJ-99", title="Fix login bug",
            body="", url="", author="", timestamp="2026-04-10T10:00:00+00:00",
            metadata={"due": "2026-04-15"},
        )
        result = agent.build_analysis_from_llm_json(
            item, "{}", scope_info=self._direct_scope(),
        )
        assert len(result.action_items) == 1
        assert "Fix login bug" in result.action_items[0].description
        assert result.action_items[0].deadline == "2026-04-15"

    def test_fyi_clears_action_items(self):
        item = RawItem(
            source="outlook", item_id="x4", title="t", body="", url="",
            author="", timestamp="2026-04-10T10:00:00+00:00",
        )
        payload = {
            "category": "fyi",
            "action_items": [{"description": "Hallucinated task", "owner": "me"}],
        }
        result = agent.build_analysis_from_llm_json(
            item, json.dumps(payload), scope_info=self._direct_scope(),
        )
        assert result.action_items == []
        assert result.has_action is False

    def test_passdown_detected_from_title(self):
        item = RawItem(
            source="outlook", item_id="p1", title="Day shift passdown",
            body="see notes below", url="", author="ops@co.com",
            timestamp="2026-04-10T10:00:00+00:00",
        )
        result = agent.build_analysis_from_llm_json(
            item, "{}", scope_info=self._direct_scope(),
        )
        assert result.is_passdown is True

    def test_project_tags_plural_key_honored(self):
        """Sync path checks data['project_tags'] before data['project_tag'].
        Batch path used to only check the singular form — this test pins
        the helper's behaviour so the drift cannot return."""
        item = RawItem(
            source="outlook", item_id="x5", title="t", body="", url="",
            author="", timestamp="2026-04-10T10:00:00+00:00",
        )
        payload = {"project_tags": ["alpha"]}
        # No projects configured → all tags pass through validator
        import config as _cfg
        _saved = _cfg.PROJECTS
        _cfg.PROJECTS = []
        try:
            result = agent.build_analysis_from_llm_json(
                item, json.dumps(payload), scope_info=self._direct_scope(),
            )
        finally:
            _cfg.PROJECTS = _saved
        # serialize_project_tags returns a string (or None)
        assert result.project_tag == "alpha"

    def test_project_tag_serialized_not_list(self):
        """Analysis.project_tag is Optional[str]; the helper must serialize."""
        item = RawItem(
            source="outlook", item_id="x6", title="t", body="", url="",
            author="", timestamp="2026-04-10T10:00:00+00:00",
        )
        payload = {"project_tag": "beta"}
        import config as _cfg
        _saved = _cfg.PROJECTS
        _cfg.PROJECTS = []
        try:
            result = agent.build_analysis_from_llm_json(
                item, json.dumps(payload), scope_info=self._direct_scope(),
            )
        finally:
            _cfg.PROJECTS = _saved
        assert isinstance(result.project_tag, (str, type(None)))
        assert result.project_tag == "beta"

    def test_metadata_fields_propagate_to_analysis(self):
        item = RawItem(
            source="outlook", item_id="x8", title="t", body="", url="",
            author="", timestamp="2026-04-10T10:00:00+00:00",
            metadata={
                "to": "user@co.com", "cc": "cc@co.com",
                "is_replied": True, "replied_at": "2026-04-10T11:00:00+00:00",
                "direction": "sent",
                "conversation_id": "C123", "conversation_topic": "Release",
            },
        )
        result = agent.build_analysis_from_llm_json(
            item, "{}", scope_info=self._direct_scope(),
        )
        assert result.to_field == "user@co.com"
        assert result.cc_field == "cc@co.com"
        assert result.is_replied is True
        assert result.replied_at == "2026-04-10T11:00:00+00:00"
        assert result.direction == "sent"
        assert result.conversation_id == "C123"
        assert result.conversation_topic == "Release"


# ── priority overrides (squire#38) ────────────────────────────────────────────

def test_priority_overrides_ctx_empty():
    import config
    prev = list(config.PRIORITY_OVERRIDES)
    config.PRIORITY_OVERRIDES = []
    try:
        assert agent._priority_overrides_ctx() == "none"
    finally:
        config.PRIORITY_OVERRIDES = prev


def test_priority_overrides_ctx_groups_by_reason():
    import config
    prev = list(config.PRIORITY_OVERRIDES)
    config.PRIORITY_OVERRIDES = [
        {"author": "boss@co.com", "project_tag": "Acme",
         "title": "Weekly sync", "llm_priority": "low",
         "user_priority": "high", "reason": "person_matters",
         "created_at": "2026-04-12T10:00:00"},
        {"author": "ops@co.com", "project_tag": "",
         "title": "Outage", "llm_priority": "medium",
         "user_priority": "high", "reason": "topic_hot",
         "created_at": "2026-04-12T11:00:00"},
    ]
    try:
        out = agent._priority_overrides_ctx()
        assert "person_matters" in out
        assert "topic_hot"       in out
        assert "boss@co.com"     in out
        assert "Acme"            in out
        assert "LLM said low, user set high" in out
    finally:
        config.PRIORITY_OVERRIDES = prev


def test_build_prompt_includes_priority_overrides():
    import config
    prev = list(config.PRIORITY_OVERRIDES)
    config.PRIORITY_OVERRIDES = [{
        "author": "boss@co.com", "project_tag": "Acme",
        "title": "Deadline incoming", "llm_priority": "low",
        "user_priority": "high", "reason": "deadline_real",
        "created_at": "2026-04-12T10:00:00",
    }]
    try:
        prompt = agent.build_prompt(_raw())
        assert "Priority overrides" in prompt
        assert "deadline_real" in prompt
        assert "Deadline incoming" in prompt
    finally:
        config.PRIORITY_OVERRIDES = prev


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

    def test_strip_safelinks_preserves_trailing_punctuation(self):
        from agent import _strip_safelinks
        body = (
            "Go to https://nam02.safelinks.protection.outlook.com/?url=x&data=y. "
            "Next sentence."
        )
        out = _strip_safelinks(body)
        assert "safelinks" not in out.lower()
        # The period that ended the URL's sentence must survive.
        assert ". Next sentence." in out

    def test_strip_safelinks_multiple_urls(self):
        from agent import _strip_safelinks
        body = (
            "See https://a.safelinks.protection.outlook.com/?x=1 and "
            "https://b.safelinks.protection.outlook.com/?y=2 for details."
        )
        out = _strip_safelinks(body)
        assert "safelinks" not in out.lower()
        assert "See  and  for details." in out or "See and for details." in out.replace("  ", " ")


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
        source="outlook",
        item_id="t1",
        title="Status",
        body=body_with_noise,
        url="",
        author="Sender <s@x.com>",
        timestamp="2026-04-21T00:00:00",
        metadata={"to": "reid.hall@prismsystems.com", "cc": "", "direction": "received"},
    )

    prompt = build_prompt(item)

    # Body-derived fields that should be gone:
    #   (Note: "-----Original Message-----" now appears in the PROMPT
    #   action-item rules as a negative-example marker; check the
    #   quoted-chain *content* instead — the From: header and body below it.)
    assert "someone@example.com" not in prompt
    assert "safelinks.protection.outlook.com" not in prompt
    assert "Reid Hall was CC'd" not in prompt
    # The real current-message directive must still be there:
    assert "are we on track for Monday?" in prompt


def test_prompt_contains_negative_owner_examples():
    """The PROMPT template must explicitly call out the three owner='me'
    traps we saw in real data (issue #83)."""
    from agent import PROMPT
    assert "quoted reply" in PROMPT.lower() or "reply chain" in PROMPT.lower()
    assert "@mention" in PROMPT.lower() or "@-mention" in PROMPT.lower()
    assert "third party" in PROMPT.lower() or "third-party" in PROMPT.lower()
