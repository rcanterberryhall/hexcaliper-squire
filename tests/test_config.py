import config


def test_validate_warns_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(config, "CF_CLIENT_ID", "")
    monkeypatch.setattr(config, "CF_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "SLACK_CLIENT_ID", "")
    monkeypatch.setattr(config, "SLACK_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "GITHUB_PAT", "")
    monkeypatch.setattr(config, "JIRA_TOKEN", "")
    monkeypatch.setattr(config, "JIRA_DOMAIN", "")
    warnings = config.validate()
    assert len(warnings) == 7


def test_validate_no_warnings_when_all_configured(monkeypatch):
    monkeypatch.setattr(config, "CF_CLIENT_ID", "real-id")
    monkeypatch.setattr(config, "CF_CLIENT_SECRET", "real-secret")
    monkeypatch.setattr(config, "SLACK_CLIENT_ID", "real-slack-id")
    monkeypatch.setattr(config, "SLACK_CLIENT_SECRET", "real-slack-secret")
    monkeypatch.setattr(config, "GITHUB_PAT", "ghp_realtoken")
    monkeypatch.setattr(config, "JIRA_TOKEN", "realtoken")
    monkeypatch.setattr(config, "JIRA_DOMAIN", "mycompany.atlassian.net")
    assert config.validate() == []


def test_validate_catches_placeholder_values(monkeypatch):
    monkeypatch.setattr(config, "CF_CLIENT_ID", "your-client-id")
    monkeypatch.setattr(config, "GITHUB_PAT", "ghp_your-token")
    warnings = config.validate()
    assert any("CF_CLIENT_ID" in w for w in warnings)
    assert any("GITHUB_PAT" in w for w in warnings)


def test_ollama_headers_without_cf(monkeypatch):
    monkeypatch.setattr(config, "CF_CLIENT_ID", "")
    monkeypatch.setattr(config, "CF_CLIENT_SECRET", "")
    h = config.ollama_headers()
    assert h == {"Content-Type": "application/json", "X-Source": "parsival"}


def test_ollama_headers_with_cf(monkeypatch):
    monkeypatch.setattr(config, "CF_CLIENT_ID", "id-123")
    monkeypatch.setattr(config, "CF_CLIENT_SECRET", "secret-456")
    h = config.ollama_headers()
    assert h["CF-Access-Client-Id"] == "id-123"
    assert h["CF-Access-Client-Secret"] == "secret-456"
    assert h["Content-Type"] == "application/json"
    assert h["X-Source"] == "parsival"
