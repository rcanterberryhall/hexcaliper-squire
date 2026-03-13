import requests
from datetime import datetime, timedelta, timezone
from models import RawItem
import config

BASE = "https://api.github.com"


def _h() -> dict:
    return {
        "Authorization":        f"Bearer {config.GITHUB_PAT}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str, params: dict = None) -> dict | list:
    r = requests.get(f"{BASE}{path}", headers=_h(), params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()


def _ts(iso: str) -> str:
    """Normalise GitHub ISO timestamps to Python-parseable format."""
    return iso.replace("Z", "+00:00") if iso else ""


def fetch() -> list[RawItem]:
    if not config.GITHUB_PAT or config.GITHUB_PAT.startswith("ghp_your"):
        print("[github] not configured — skipping")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.LOOKBACK_HOURS)
    items: list[RawItem] = []
    seen:  set[str]      = set()

    # 1. Notifications (mentions, review requests, assignments, CI failures)
    try:
        for n in _get("/notifications", {"all": False, "participating": True, "per_page": 50}):
            if datetime.fromisoformat(_ts(n["updated_at"])) < cutoff:
                continue
            subj     = n.get("subject", {})
            repo     = n["repository"]["full_name"]
            reason   = n.get("reason", "")
            body     = f"Repo: {repo}\nReason: {reason}\nTitle: {subj.get('title', '')}"
            html_url = ""
            try:
                url = subj.get("url", "")
                if url:
                    detail   = _get(url.replace(BASE, ""))
                    body     = f"Repo: {repo}\nReason: {reason}\n\n{detail.get('body') or body}"
                    html_url = detail.get("html_url", "")
            except Exception:
                pass
            nid = str(n["id"])
            seen.add(nid)
            items.append(RawItem(
                source    = "github",
                item_id   = nid,
                title     = f"[{repo}] {subj.get('title', '')}",
                body      = body[:3000],
                url       = html_url or "https://github.com/notifications",
                author    = repo,
                timestamp = _ts(n["updated_at"]),
                metadata  = {"reason": reason, "type": subj.get("type", ""), "repo": repo},
            ))
    except Exception as e:
        print(f"[github] notifications: {e}")

    # 2. PRs requesting my review
    try:
        results = _get("/search/issues", {
            "q":        f"is:open is:pr review-requested:{config.GITHUB_USERNAME}",
            "per_page": 20,
            "sort":     "updated",
        }).get("items", [])
        for pr in results:
            if datetime.fromisoformat(_ts(pr["updated_at"])) < cutoff:
                continue
            pid = f"pr_{pr['id']}"
            if pid in seen:
                continue
            seen.add(pid)
            items.append(RawItem(
                source    = "github",
                item_id   = pid,
                title     = f"[PR] {pr['title']}",
                body      = f"PR #{pr['number']}\n\n{pr.get('body') or ''}",
                url       = pr["html_url"],
                author    = pr["user"]["login"],
                timestamp = _ts(pr["updated_at"]),
                metadata  = {"type": "pull_request", "number": pr["number"]},
            ))
    except Exception as e:
        print(f"[github] PR search: {e}")

    # 3. Open issues assigned to me
    try:
        for issue in _get("/issues", {
            "filter":   "assigned",
            "state":    "open",
            "per_page": 30,
            "sort":     "updated",
        }):
            if datetime.fromisoformat(_ts(issue["updated_at"])) < cutoff:
                continue
            iid = f"issue_{issue['id']}"
            if iid in seen:
                continue
            seen.add(iid)
            repo = issue.get("repository", {}).get("full_name", "")
            items.append(RawItem(
                source    = "github",
                item_id   = iid,
                title     = f"[Issue] {issue['title']}",
                body      = f"Issue #{issue['number']}\nRepo: {repo}\n\n{issue.get('body') or ''}",
                url       = issue["html_url"],
                author    = issue["user"]["login"],
                timestamp = _ts(issue["updated_at"]),
                metadata  = {"type": "issue", "repo": repo},
            ))
    except Exception as e:
        print(f"[github] issues: {e}")

    print(f"[github] {len(items)} items")
    return items
