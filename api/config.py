"""
config.py — Runtime configuration for the Squire API.

All values are read from environment variables on startup.  The
``apply_overrides`` function allows settings saved via the ``/settings``
endpoint to hot-reload config without restarting the container.

Module-level variables (populated at import and updated by ``apply_overrides``):

Ollama / Hexcaliper:
    ``OLLAMA_URL``, ``OLLAMA_MODEL``, ``CF_CLIENT_ID``, ``CF_CLIENT_SECRET``

Slack:
    ``SLACK_CLIENT_ID``, ``SLACK_CLIENT_SECRET``, ``SLACK_USER_TOKENS``,
    ``SLACK_BOT_TOKEN``, ``SLACK_CHANNELS``

Microsoft Teams:
    ``TEAMS_CLIENT_ID``, ``TEAMS_CLIENT_SECRET``, ``TEAMS_USER_TOKENS``

GitHub:
    ``GITHUB_PAT``, ``GITHUB_USERNAME``

Jira:
    ``JIRA_EMAIL``, ``JIRA_TOKEN``, ``JIRA_DOMAIN``, ``JIRA_JQL``

User / project / topic context:
    ``USER_NAME`` — display name used in LLM prompts and Slack pre-filtering.
    ``USER_EMAIL`` — email address used in LLM prompts and Slack pre-filtering.
    ``FOCUS_TOPICS`` — list of general watch-topic keywords (populated from a
    comma-separated ``FOCUS_TOPICS`` env var).
    ``PROJECTS`` — list of project dicts, each with the following keys:
        ``name``             — unique project identifier shown in the UI and used
                               as the ``project_tag`` on analysis records.
        ``keywords``         — static keywords configured by the user.
        ``channels``         — Slack/Teams channel names to pre-filter by.
        ``description``      — optional free-text description passed to the LLM
                               to help it distinguish projects with similar names.
        ``parent``           — optional parent project name; when set and valid,
                               the LLM prompt notes the sub-project relationship.
        ``senders``          — static list of email addresses or group aliases
                               associated with this project.
        ``learned_keywords`` — keywords grown at runtime via the tagging workflow.
        ``learned_senders``  — email addresses grown at runtime via the tagging
                               workflow.
    Populated from a JSON-encoded ``PROJECTS`` env var; ``learned_keywords``
    and ``learned_senders`` are grown at runtime via ``apply_overrides``.
    ``NOISE_KEYWORDS`` — list of keyword strings learned from items that have
    been marked as irrelevant; not set via env var, only via ``apply_overrides``.

App:
    ``PAGE_API_PORT``, ``DB_PATH``, ``LOOKBACK_HOURS``
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

OLLAMA_URL   = _get("OLLAMA_URL",   "http://host.docker.internal:11400/api/generate")
OLLAMA_MODEL = _get("OLLAMA_MODEL", "qwen3:30b-a3b")
MERLLM_URL   = _get("MERLLM_URL",   "http://host.docker.internal:11400")

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

# ── Microsoft Teams ──────────────────────────────────────────────────────────

TEAMS_CLIENT_ID     = _get("TEAMS_CLIENT_ID")
TEAMS_CLIENT_SECRET = _get("TEAMS_CLIENT_SECRET")

# Per-account user tokens stored after OAuth — populated at runtime via apply_overrides.
TEAMS_USER_TOKENS: list[dict] = []

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

# ── User / Project / Topic context ───────────────────────────────────────────

USER_NAME  = _get("USER_NAME", "")
USER_EMAIL = _get("USER_EMAIL", "")

_ft          = _get("FOCUS_TOPICS", "")
FOCUS_TOPICS: list[str] = [t.strip() for t in _ft.split(",") if t.strip()] if _ft else []

import json as _json
try:
    PROJECTS: list[dict] = _json.loads(_get("PROJECTS", "[]"))
except Exception:
    PROJECTS: list[dict] = []

NOISE_KEYWORDS:        list[str]  = []
TASK_KEYWORDS:         list[str]  = []
APPROVAL_KEYWORDS:     list[str]  = []
FYI_KEYWORDS:          list[str]  = []
# Correction examples grown from manual re-assignments.
# Each entry: {description, llm_owner, corrected_to}
ASSIGNMENT_CORRECTIONS: list[dict] = []

# ── App ───────────────────────────────────────────────────────────────────────

PAGE_API_PORT  = int(_get("PAGE_API_PORT", "8001"))
DB_PATH        = _get("DB_PATH", "/app/data/squire.db")
LOOKBACK_HOURS = int(_get("LOOKBACK_HOURS", "48"))


def apply_overrides(d: dict) -> None:
    """
    Hot-reload config from a saved-settings dict without restarting the container.

    Called on startup (if saved settings exist in the DB) and after every
    successful ``POST /settings`` or OAuth callback.  Handles all string
    credential fields, ``slack_user_tokens``, ``teams_user_tokens``,
    ``slack_channels``, ``focus_topics``, ``projects``, ``noise_keywords``,
    and ``lookback_hours``.

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
        "teams_client_id":      "TEAMS_CLIENT_ID",
        "teams_client_secret":  "TEAMS_CLIENT_SECRET",
        "github_pat":           "GITHUB_PAT",
        "github_username":      "GITHUB_USERNAME",
        "jira_email":           "JIRA_EMAIL",
        "jira_token":           "JIRA_TOKEN",
        "jira_domain":          "JIRA_DOMAIN",
        "jira_jql":             "JIRA_JQL",
        "user_name":            "USER_NAME",
        "user_email":           "USER_EMAIL",
    }
    for key, var in str_fields.items():
        if key in d and d[key] is not None:
            setattr(mod, var, str(d[key]))
    if "slack_user_tokens" in d and isinstance(d["slack_user_tokens"], list):
        setattr(mod, "SLACK_USER_TOKENS", d["slack_user_tokens"])
    if "teams_user_tokens" in d and isinstance(d["teams_user_tokens"], list):
        setattr(mod, "TEAMS_USER_TOKENS", d["teams_user_tokens"])
    if "slack_channels" in d:
        sc = d["slack_channels"] or ""
        setattr(mod, "SLACK_CHANNELS", [c.strip() for c in sc.split(",") if c.strip()])
    if "focus_topics" in d:
        ft = d["focus_topics"] or ""
        setattr(mod, "FOCUS_TOPICS", [t.strip() for t in ft.split(",") if t.strip()])
    if "projects" in d and isinstance(d["projects"], list):
        setattr(mod, "PROJECTS", d["projects"])
    if "noise_keywords" in d and isinstance(d["noise_keywords"], list):
        setattr(mod, "NOISE_KEYWORDS", d["noise_keywords"])
    if "task_keywords" in d and isinstance(d["task_keywords"], list):
        setattr(mod, "TASK_KEYWORDS", d["task_keywords"])
    if "approval_keywords" in d and isinstance(d["approval_keywords"], list):
        setattr(mod, "APPROVAL_KEYWORDS", d["approval_keywords"])
    if "fyi_keywords" in d and isinstance(d["fyi_keywords"], list):
        setattr(mod, "FYI_KEYWORDS", d["fyi_keywords"])
    if "assignment_corrections" in d and isinstance(d["assignment_corrections"], list):
        setattr(mod, "ASSIGNMENT_CORRECTIONS", d["assignment_corrections"])
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
        (SLACK_CLIENT_ID,     "your-slack-client",  "SLACK_CLIENT_ID not configured"),
        (SLACK_CLIENT_SECRET, "your-slack-client",  "SLACK_CLIENT_SECRET not configured"),
        (GITHUB_PAT,       "ghp_your",                  "GITHUB_PAT not configured"),
        (JIRA_TOKEN,       "your-jira-api-token",       "JIRA_TOKEN not configured"),
        (JIRA_DOMAIN,      "yourcompany.atlassian.net", "JIRA_DOMAIN not configured"),
    ]
    for val, sentinel, msg in checks:
        if not val or val.startswith(sentinel):
            warnings.append(msg)
    return warnings
