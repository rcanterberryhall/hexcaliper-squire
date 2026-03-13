from unittest.mock import patch, MagicMock

import connector_github
import connector_slack
import connector_jira
import connector_outlook
import config


# ── GitHub ────────────────────────────────────────────────────────────────────

def test_github_skips_when_not_configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_PAT", "")
    assert connector_github.fetch() == []


def test_github_skips_on_placeholder_pat(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_PAT", "ghp_your-placeholder")
    assert connector_github.fetch() == []


def test_github_ts_normalises_z():
    assert connector_github._ts("2024-01-15T10:00:00Z") == "2024-01-15T10:00:00+00:00"


def test_github_ts_empty_string():
    assert connector_github._ts("") == ""


def test_github_fetch_returns_items(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_PAT", "ghp_realtoken")
    monkeypatch.setattr(config, "GITHUB_USERNAME", "alice")
    monkeypatch.setattr(config, "LOOKBACK_HOURS", 48)

    now = "2026-03-13T12:00:00Z"

    notifications = [
        {
            "id": "n1",
            "updated_at": now,
            "subject": {"title": "PR merged", "url": None, "type": "PullRequest"},
            "repository": {"full_name": "org/repo"},
            "reason": "mention",
        }
    ]

    def fake_get(path, params=None):
        if path == "/notifications":
            return notifications
        if path == "/search/issues":
            return {"items": []}
        if path == "/issues":
            return []
        return {}

    with patch("connector_github.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.side_effect = fake_get
        # Match path to response
        mock_get.return_value = mock_resp

        with patch("connector_github._get", side_effect=fake_get):
            items = connector_github.fetch()

    assert len(items) == 1
    assert items[0].source == "github"
    assert items[0].item_id == "n1"


# ── Slack ─────────────────────────────────────────────────────────────────────

def test_slack_skips_when_not_configured(monkeypatch):
    monkeypatch.setattr(config, "SLACK_BOT_TOKEN", "")
    assert connector_slack.fetch() == []


def test_slack_skips_on_placeholder_token(monkeypatch):
    monkeypatch.setattr(config, "SLACK_BOT_TOKEN", "xoxb-your-placeholder")
    assert connector_slack.fetch() == []


def test_slack_returns_empty_on_api_error(monkeypatch):
    monkeypatch.setattr(config, "SLACK_BOT_TOKEN", "xoxb-realtoken")
    monkeypatch.setattr(config, "LOOKBACK_HOURS", 48)
    monkeypatch.setattr(config, "SLACK_CHANNELS", [])

    with patch("connector_slack.requests.get", side_effect=Exception("connection refused")):
        items = connector_slack.fetch()

    assert items == []


# ── Jira ──────────────────────────────────────────────────────────────────────

def test_jira_skips_when_not_configured(monkeypatch):
    monkeypatch.setattr(config, "JIRA_TOKEN", "")
    monkeypatch.setattr(config, "JIRA_DOMAIN", "")
    assert connector_jira.fetch() == []


def test_jira_skips_on_placeholder_domain(monkeypatch):
    monkeypatch.setattr(config, "JIRA_TOKEN", "realtoken")
    monkeypatch.setattr(config, "JIRA_DOMAIN", "yourcompany.atlassian.net")
    assert connector_jira.fetch() == []


def test_jira_text_extraction_from_adf():
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Hello"},
                {"type": "text", "text": " world"},
            ]}
        ]
    }
    result = connector_jira._text(adf)
    assert "Hello" in result
    assert "world" in result


def test_jira_text_empty_input():
    assert connector_jira._text(None) == ""
    assert connector_jira._text("") == ""


def test_jira_text_plain_string():
    assert connector_jira._text("plain text") == "plain text"


def test_jira_fetch_returns_items(monkeypatch):
    monkeypatch.setattr(config, "JIRA_TOKEN", "realtoken")
    monkeypatch.setattr(config, "JIRA_DOMAIN", "mycompany.atlassian.net")
    monkeypatch.setattr(config, "JIRA_JQL", "assignee = currentUser()")
    monkeypatch.setattr(config, "LOOKBACK_HOURS", 48)

    fake_response = {
        "issues": [{
            "key": "PROJ-1",
            "fields": {
                "summary": "Fix the thing",
                "description": None,
                "status": {"name": "In Progress"},
                "priority": {"name": "High"},
                "reporter": {"displayName": "Bob"},
                "updated": "2026-03-13T10:00:00+00:00",
                "duedate": "2026-03-20",
                "comment": {"comments": []},
                "issuetype": {"name": "Story"},
                "project": {"name": "My Project"},
            }
        }]
    }

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = fake_response

    with patch("connector_jira.requests.get", return_value=mock_resp):
        items = connector_jira.fetch()

    assert len(items) == 1
    assert items[0].item_id == "PROJ-1"
    assert items[0].source == "jira"
    assert "Fix the thing" in items[0].title
    assert items[0].metadata["due"] == "2026-03-20"


# ── Outlook ───────────────────────────────────────────────────────────────────

def test_outlook_always_returns_empty():
    assert connector_outlook.fetch() == []
