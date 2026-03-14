"""
config.py — Runtime configuration for the Squire API.

All values are read from environment variables on startup.  The
``apply_overrides`` function allows settings saved via the ``/settings``
endpoint to hot-reload config without restarting the container.
"""
import os


def _get(key: str, default: str = "") -> str:
    """
    Read an environment variable, stripping surrounding whitespace.

    :param key: Environment variable name.
    :type key: str
    :param default: Value to return if the variable is not set.
    :type default: str
    :return: The variable's value, or ``default`` if absent.
    :rtype: str
    """
    return os.environ.get(key, default).strip()


# ── Ollama / Hexcaliper ───────────────────────────────────────────────────────

OLLAMA_URL   = _get("OLLAMA_URL",   "https://ollama.hexcaliper.com/api/generate")
OLLAMA_MODEL = _get("OLLAMA_MODEL", "llama3.2")

# Cloudflare Access service token for authenticating requests to Ollama.
CF_CLIENT_ID     = _get("CF_CLIENT_ID")
CF_CLIENT_SECRET = _get("CF_CLIENT_SECRET")

# ── Slack ─────────────────────────────────────────────────────────────────────

SLACK_CLIENT_ID     = _get("SLACK_CLIENT_ID")
SLACK_CLIENT_SECRET = _get("SLACK_CLIENT_SECRET")

# Per-workspace user tokens stored after OAuth — populated at runtime via apply_overrides.
SLACK_USER_TOKENS: list[dict] = []

# Legacy bot token kept for backward compatibility.
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


def apply_overrides(d: dict) -> None:
    """
    Hot-reload config from a saved-settings dict without restarting the container.

    Called on startup (if saved settings exist in the DB) and after every
    successful ``POST /settings`` request.

    :param d: Dict of setting key/value pairs, as stored in the ``settings``
              TinyDB table or posted by the frontend.
    :type d: dict
    """
    import sys
    mod = sys.modules[__name__]
    str_fields = {
        "ollama_url":           "OLLAMA_URL",
        "ollama_model":         "OLLAMA_MODEL",
        "cf_client_id":         "CF_CLIENT_ID",
        "cf_client_secret":     "CF_CLIENT_SECRET",
        "slack_client_id":      "SLACK_CLIENT_ID",
        "slack_client_secret":  "SLACK_CLIENT_SECRET",
        "slack_bot_token":      "SLACK_BOT_TOKEN",
        "github_pat":           "GITHUB_PAT",
        "github_username":      "GITHUB_USERNAME",
        "jira_email":           "JIRA_EMAIL",
        "jira_token":           "JIRA_TOKEN",
        "jira_domain":          "JIRA_DOMAIN",
        "jira_jql":             "JIRA_JQL",
    }
    for key, var in str_fields.items():
        if key in d and d[key] is not None:
            setattr(mod, var, str(d[key]))
    if "slack_user_tokens" in d and isinstance(d["slack_user_tokens"], list):
        setattr(mod, "SLACK_USER_TOKENS", d["slack_user_tokens"])
    if "slack_channels" in d:
        sc = d["slack_channels"] or ""
        setattr(mod, "SLACK_CHANNELS", [c.strip() for c in sc.split(",") if c.strip()])
    if "lookback_hours" in d and d["lookback_hours"] is not None:
        setattr(mod, "LOOKBACK_HOURS", int(d["lookback_hours"]))


def ollama_headers() -> dict:
    """
    Build request headers for Ollama API calls.

    Includes Cloudflare Access service token headers when both
    ``CF_CLIENT_ID`` and ``CF_CLIENT_SECRET`` are configured, allowing
    requests to pass through a Cloudflare Access policy protecting the
    Ollama endpoint.

    :return: Dict of HTTP headers to include with every Ollama request.
    :rtype: dict
    """
    h = {"Content-Type": "application/json"}
    if CF_CLIENT_ID and CF_CLIENT_SECRET:
        h["CF-Access-Client-Id"]     = CF_CLIENT_ID
        h["CF-Access-Client-Secret"] = CF_CLIENT_SECRET
    return h


def validate() -> list[str]:
    """
    Return a list of warnings for missing or placeholder configuration values.

    Used by the ``/health`` and ``/settings`` endpoints to surface
    integration issues to the frontend without raising exceptions.

    :return: List of human-readable warning strings, empty if fully configured.
    :rtype: list[str]
    """
    warnings = []
    checks = [
        (CF_CLIENT_ID,     "your-client-id",           "CF_CLIENT_ID not set — Ollama requests will be unauthenticated"),
        (CF_CLIENT_SECRET, "your-client-secret",        "CF_CLIENT_SECRET not set — Ollama requests will be unauthenticated"),
        (SLACK_CLIENT_ID,  "",  "SLACK_CLIENT_ID not configured"),
        (SLACK_CLIENT_SECRET, "", "SLACK_CLIENT_SECRET not configured"),
        (GITHUB_PAT,       "ghp_your",                  "GITHUB_PAT not configured"),
        (JIRA_TOKEN,       "your-jira-api-token",       "JIRA_TOKEN not configured"),
        (JIRA_DOMAIN,      "yourcompany.atlassian.net", "JIRA_DOMAIN not configured"),
    ]
    for val, sentinel, msg in checks:
        if not val or val.startswith(sentinel):
            warnings.append(msg)
    return warnings
