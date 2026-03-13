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
    with patch("agent.requests.post", return_value=_mock_response(payload)):
        result = agent.analyze(_raw())

    assert result.has_action is True
    assert result.priority == "high"
    assert result.category == "task"
    assert result.summary == "Deploy the release"
    assert len(result.action_items) == 1
    assert result.action_items[0].description == "Ship it"
    assert result.action_items[0].deadline == "2024-03-01"


def test_analyze_defaults_on_empty_response():
    with patch("agent.requests.post", return_value=_mock_response({})):
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
    with patch("agent.requests.post", return_value=_mock_response({})):
        result = agent.analyze(item)

    assert len(result.action_items) == 1
    assert "Fix login bug" in result.action_items[0].description
    assert result.action_items[0].deadline == "2024-04-01"


def test_analyze_skips_action_items_without_description():
    payload = {
        "has_action": True,
        "priority": "low",
        "category": "fyi",
        "action_items": [{"description": "", "deadline": None, "owner": "me"}],
        "summary": "Nothing to do",
        "urgency_reason": None,
    }
    with patch("agent.requests.post", return_value=_mock_response(payload)):
        result = agent.analyze(_raw())

    assert result.action_items == []


def test_analyze_batch_calls_progress_cb():
    items = [_raw(item_id=str(i)) for i in range(3)]
    calls = []

    with patch("agent.requests.post", return_value=_mock_response({})):
        agent.analyze_batch(items, progress_cb=lambda i, t, s, title: calls.append(i))

    assert calls == [0, 1, 2]


def test_analyze_batch_skips_failed_items():
    items = [_raw(item_id="ok"), _raw(item_id="bad"), _raw(item_id="ok2")]

    def side_effect(*args, **kwargs):
        m = MagicMock()
        m.raise_for_status.side_effect = Exception("network error")
        return m

    with patch("agent.requests.post", side_effect=side_effect):
        results = agent.analyze_batch(items)

    # Errors are swallowed; no results but no crash
    assert results == []
