import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta, timezone
from models import RawItem
import config


def _auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(config.JIRA_EMAIL, config.JIRA_TOKEN)


def _base() -> str:
    return f"https://{config.JIRA_DOMAIN}/rest/api/3"


def _text(adf) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if not adf:
        return ""
    if isinstance(adf, str):
        return adf
    if adf.get("type") == "text":
        return adf.get("text", "")
    return " ".join(filter(None, (_text(n) for n in adf.get("content", [])))).strip()


def fetch() -> list[RawItem]:
    if not config.JIRA_TOKEN or not config.JIRA_DOMAIN:
        print("[jira] not configured — skipping")
        return []
    if config.JIRA_DOMAIN == "yourcompany.atlassian.net":
        print("[jira] domain is placeholder — skipping")
        return []

    items: list[RawItem] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.LOOKBACK_HOURS)

    try:
        r = requests.get(
            f"{_base()}/search",
            auth    = _auth(),
            params  = {
                "jql":        config.JIRA_JQL,
                "maxResults": 50,
                "fields":     "summary,description,status,priority,reporter,updated,duedate,comment,issuetype,project",
            },
            headers = {"Accept": "application/json"},
            timeout = 15,
        )
        r.raise_for_status()

        for issue in r.json().get("issues", []):
            f        = issue["fields"]
            updated  = datetime.fromisoformat(f["updated"].replace("Z", "+00:00"))
            desc     = _text(f.get("description"))
            status   = f.get("status", {}).get("name", "")
            priority = f.get("priority", {}).get("name", "Medium")
            due      = f.get("duedate", "")
            project  = f.get("project", {}).get("name", "")
            reporter = f.get("reporter", {}).get("displayName", "")

            comments     = f.get("comment", {}).get("comments", [])
            last_comment = ""
            if comments:
                lc = comments[-1]
                last_comment = (
                    f"\nLatest comment "
                    f"({lc.get('author', {}).get('displayName', '')}):"
                    f" {_text(lc.get('body'))}"
                )

            body = (
                f"Project: {project}\nStatus: {status}\nPriority: {priority}\n"
                f"Due: {due or 'not set'}\nReporter: {reporter}\n\n"
                f"{desc}{last_comment}"
            )

            items.append(RawItem(
                source    = "jira",
                item_id   = issue["key"],
                title     = f"[{issue['key']}] {f.get('summary', '')}",
                body      = body[:3000],
                url       = f"https://{config.JIRA_DOMAIN}/browse/{issue['key']}",
                author    = reporter,
                timestamp = f["updated"],
                metadata  = {
                    "status":   status,
                    "priority": priority,
                    "due":      due,
                    "project":  project,
                    "is_recent": updated > cutoff,
                },
            ))

    except Exception as e:
        print(f"[jira] error: {e}")

    print(f"[jira] {len(items)} issues")
    return items
