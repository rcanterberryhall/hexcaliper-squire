import os


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# ── Ollama / Hexcaliper ───────────────────────────────────────────────────────
OLLAMA_URL   = _get("OLLAMA_URL",   "https://ollama.hexcaliper.com/api/generate")
OLLAMA_MODEL = _get("OLLAMA_MODEL", "llama3.2")

CF_CLIENT_ID     = _get("CF_CLIENT_ID")
CF_CLIENT_SECRET = _get("CF_CLIENT_SECRET")

# ── Slack ─────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = _get("SLACK_BOT_TOKEN")
_sc             = _get("SLACK_CHANNELS")
SLACK_CHANNELS  = [c.strip() for c in _sc.split(",") if c.strip()] if _sc else []

# ── GitHub ────────────────────────────────────────────────────────────────────
GITHUB_PAT      = _get("GITHUB_PAT")
GITHUB_USERNAME = _get("GITHUB_USERNAME")

# ── Jira Cloud ────────────────────────────────────────────────────────────────
JIRA_EMAIL  = _get("JIRA_EMAIL")
JIRA_TOKEN  = _get("JIRA_TOKEN")
JIRA_DOMAIN = _get("JIRA_DOMAIN")
JIRA_JQL    = _get(
    "JIRA_JQL",
    "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC",
)

# ── App ───────────────────────────────────────────────────────────────────────
PAGE_API_PORT  = int(_get("PAGE_API_PORT", "8001"))
DB_PATH        = _get("DB_PATH", "/app/data/page.db")
LOOKBACK_HOURS = int(_get("LOOKBACK_HOURS", "48"))


def ollama_headers() -> dict:
    """Cloudflare Access service token headers for every Ollama request."""
    h = {"Content-Type": "application/json"}
    if CF_CLIENT_ID and CF_CLIENT_SECRET:
        h["CF-Access-Client-Id"]     = CF_CLIENT_ID
        h["CF-Access-Client-Secret"] = CF_CLIENT_SECRET
    return h


def validate() -> list[str]:
    """Return warnings for missing or placeholder config values."""
    warnings = []
    checks = [
        (CF_CLIENT_ID,     "your-client-id",           "CF_CLIENT_ID not set — Ollama requests will be unauthenticated"),
        (CF_CLIENT_SECRET, "your-client-secret",        "CF_CLIENT_SECRET not set — Ollama requests will be unauthenticated"),
        (SLACK_BOT_TOKEN,  "xoxb-your",                 "SLACK_BOT_TOKEN not configured"),
        (GITHUB_PAT,       "ghp_your",                  "GITHUB_PAT not configured"),
        (JIRA_TOKEN,       "your-jira-api-token",       "JIRA_TOKEN not configured"),
        (JIRA_DOMAIN,      "yourcompany.atlassian.net", "JIRA_DOMAIN not configured"),
    ]
    for val, sentinel, msg in checks:
        if not val or val.startswith(sentinel):
            warnings.append(msg)
    return warnings
