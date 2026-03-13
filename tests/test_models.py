from models import RawItem, ActionItem, Analysis


def test_raw_item_defaults():
    item = RawItem(
        source="github", item_id="abc", title="T", body="B",
        url="http://x", author="alice", timestamp="2024-01-01T00:00:00",
    )
    assert item.metadata == {}
    assert item.source == "github"


def test_action_item_optional_deadline():
    a = ActionItem(description="Do something", deadline=None, owner="me")
    assert a.deadline is None


def test_analysis_fields():
    a = Analysis(
        item_id="x", source="slack", title="T", author="bob",
        timestamp="2024-01-01", url="", has_action=True,
        priority="high", category="task",
        action_items=[ActionItem("Fix it", "2024-02-01", "me")],
        summary="A task", urgency_reason="overdue",
    )
    assert a.has_action is True
    assert len(a.action_items) == 1
    assert a.action_items[0].owner == "me"
