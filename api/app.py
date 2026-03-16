"""
app.py — Hexcaliper Squire FastAPI application.

Exposes the REST API consumed by the frontend and the host sidecar scripts.
Key responsibilities:

- Receiving and deduplicating raw items via ``POST /ingest`` (sidecar path).
- Orchestrating multi-source scans via ``POST /scan`` (frontend path).
- Persisting ``Analysis`` and ``Todo`` records to TinyDB, including the
  context-aware enrichment fields: ``hierarchy``, ``is_passdown``,
  ``project_tag``, ``goals``, ``key_dates``, and ``body_preview``.
- Serving settings, stats, and Slack OAuth endpoints to the frontend.
- Project and noise learning:
    - ``POST /analyses/{item_id}/tag`` — tags an item to a project and
      triggers background LLM keyword extraction and sender/group address
      extraction, merging results into the project's ``learned_keywords``
      and ``learned_senders`` lists in settings.
    - ``POST /analyses/{item_id}/noise`` — marks an item as irrelevant and
      triggers background keyword extraction into ``config.NOISE_KEYWORDS``.
- ``POST /reset`` — truncates the analyses, todos, and scan_logs tables while
  preserving saved settings.
- ``GET /projects`` — returns configured projects with learned keyword counts.
- ``GET /settings`` now returns ``user_name``, ``user_email``, ``focus_topics``,
  ``projects``, and ``noise_keywords`` in addition to all credential fields.

All AI analysis is performed asynchronously via ``agent.analyze`` so that
HTTP responses are returned immediately and the UI polls for results.
"""
import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional

import requests as http_requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from tinydb import TinyDB, Query

import config
from agent import analyze, extract_keywords, extract_emails
from models import RawItem, Analysis
import connector_slack
import connector_github
import connector_jira
import connector_outlook

app = FastAPI(title="Hexcaliper Squire API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)

# Ensure data directory exists before TinyDB opens the file
os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)

db           = TinyDB(config.DB_PATH)
analyses     = db.table("analyses")
todos        = db.table("todos")
scan_logs    = db.table("scan_logs")
settings_tbl = db.table("settings")
Q            = Query()
db_lock      = threading.Lock()

# Hot-load any previously saved settings on startup
_saved_settings = settings_tbl.get(doc_id=1)
if _saved_settings:
    config.apply_overrides(_saved_settings)

CONNECTORS = {
    "slack":   connector_slack,
    "github":  connector_github,
    "jira":    connector_jira,
    "outlook": connector_outlook,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_user(request: Request) -> str:
    """Mirror hexcaliper's Cloudflare Access user scoping."""
    return request.headers.get("cf-access-authenticated-user-email", "local@dev")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Scan state ────────────────────────────────────────────────────────────────

scan_state: dict = {
    "running":        False,
    "cancelled":      False,
    "progress":       0,
    "total":          0,
    "current_source": "",
    "current_item":   "",
    "message":        "idle",
    "ingest_pending": 0,
}


def _save_analysis(a: Analysis) -> None:
    """
    Upsert an ``Analysis`` into TinyDB and create todo rows for action items.

    Stores all base fields plus the context-aware enrichment fields:
    ``hierarchy``, ``is_passdown``, ``project_tag``, ``goals`` (JSON),
    ``key_dates`` (JSON), and ``body_preview``.  Todo rows are only inserted
    for action items that do not already exist for the same ``item_id`` and
    ``description`` pair.
    """
    with db_lock:
        analyses.upsert(
            {
                "item_id":      a.item_id,
                "source":       a.source,
                "title":        a.title,
                "author":       a.author,
                "timestamp":    a.timestamp,
                "url":          a.url,
                "has_action":   a.has_action,
                "priority":     a.priority,
                "category":     a.category,
                "summary":      a.summary,
                "urgency":      a.urgency_reason,
                "action_items": json.dumps([
                    {"description": x.description, "deadline": x.deadline, "owner": x.owner}
                    for x in a.action_items
                ]),
                "hierarchy":    a.hierarchy,
                "is_passdown":  a.is_passdown,
                "project_tag":  a.project_tag,
                "goals":        json.dumps(a.goals),
                "key_dates":    json.dumps(a.key_dates),
                "body_preview": a.body_preview,
                "to_field":     a.to_field,
                "cc_field":     a.cc_field,
                "is_replied":   a.is_replied,
                "replied_at":   a.replied_at,
                "processed_at": now_iso(),
            },
            Q.item_id == a.item_id,
        )

        if a.has_action:
            for item in a.action_items:
                exists = todos.get(
                    (Q.item_id == a.item_id) & (Q.description == item.description)
                )
                if not exists:
                    todos.insert({
                        "item_id":     a.item_id,
                        "source":      a.source,
                        "title":       a.title,
                        "url":         a.url,
                        "description": item.description,
                        "deadline":    item.deadline,
                        "owner":       item.owner,
                        "priority":    a.priority,
                        "done":        False,
                        "created_at":  now_iso(),
                    })


def _run_scan(sources: list[str]) -> None:
    scan_state.update({
        "running": True, "cancelled": False,
        "progress": 0, "total": 0, "message": "Starting...",
    })
    started   = now_iso()
    all_items: list[RawItem] = []

    for src in sources:
        if scan_state["cancelled"]:
            break
        scan_state.update({"message": f"Fetching {src}...", "current_source": src})
        connector = CONNECTORS.get(src)
        if connector:
            all_items.extend(connector.fetch())

    scan_state["total"]   = len(all_items)
    scan_state["message"] = f"Analyzing {len(all_items)} items..."

    results = []
    try:
        for i, item in enumerate(all_items):
            if scan_state["cancelled"]:
                break
            scan_state.update({
                "progress":       i,
                "current_source": item.source,
                "current_item":   item.title[:60],
                "message":        f"[{item.source}] {i + 1}/{len(all_items)}: {item.title[:60]}",
            })
            try:
                results.append(analyze(item))
            except Exception as e:
                print(f"[agent] {item.item_id}: {e}")

        actions = sum(1 for r in results if r.has_action)
        for r in results:
            _save_analysis(r)
        status = "cancelled" if scan_state["cancelled"] else "success"
        with db_lock:
            scan_logs.insert({
                "started_at":    started,
                "finished_at":   now_iso(),
                "sources":       ",".join(sources),
                "items_scanned": len(results),
                "actions_found": actions,
                "status":        status,
            })
        if scan_state["cancelled"]:
            scan_state["message"] = (
                f"Stopped — {actions} action items found in "
                f"{len(results)}/{len(all_items)} items processed."
            )
        else:
            scan_state["message"] = (
                f"Done — {actions} action items found across "
                f"{len(all_items)} items from {', '.join(sources)}."
            )
    except Exception as e:
        with db_lock:
            scan_logs.insert({
                "started_at":    started,
                "finished_at":   now_iso(),
                "sources":       ",".join(sources),
                "items_scanned": len(results),
                "actions_found": 0,
                "status":        f"error: {e}",
            })
        scan_state["message"] = f"Error: {e}"
    finally:
        scan_state["running"]  = False
        scan_state["progress"] = scan_state["total"]


# ── Routes ────────────────────────────────────────────────────────────────────

_MASK = "•"

def _mask(val: str) -> str:
    if not val:
        return ""
    visible = min(4, len(val))
    return val[:visible] + _MASK * max(0, len(val) - visible)


@app.get("/health")
def health():
    """Service health check — mirrors hexcaliper's /health response shape."""
    return {"ok": True, "warnings": config.validate()}


@app.post("/reset")
def reset_db():
    """Drop all analyses, todos, and scan logs. Settings are preserved."""
    with db_lock:
        analyses.truncate()
        todos.truncate()
        scan_logs.truncate()
    return {"ok": True}


@app.get("/projects")
def get_projects():
    """Return configured projects with learned keyword and sender counts."""
    return [
        {
            "name":               p.get("name", ""),
            "keywords":           p.get("keywords", []),
            "channels":           p.get("channels", []),
            "learned_keywords":   p.get("learned_keywords", []),
            "learned_count":      len(p.get("learned_keywords", [])),
            "learned_senders":    p.get("learned_senders", []),
            "sender_count":       len(p.get("learned_senders", [])),
        }
        for p in config.PROJECTS
    ]


class TagRequest(BaseModel):
    project: str


@app.patch("/analyses/{item_id}")
def patch_analysis(item_id: str, body: dict):
    """Update priority and/or category on a stored analysis. Also syncs priority to todos."""
    allowed_priorities = {"high", "medium", "low"}
    allowed_categories = {"reply_needed", "task", "deadline", "review", "approval", "fyi", "noise"}
    updates = {}
    if "priority" in body and body["priority"] in allowed_priorities:
        updates["priority"] = body["priority"]
    if "category" in body and body["category"] in allowed_categories:
        updates["category"] = body["category"]
        if body["category"] == "noise":
            updates["has_action"] = False
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update.")
    with db_lock:
        if not analyses.get(Q.item_id == item_id):
            raise HTTPException(status_code=404, detail="Item not found")
        analyses.update(updates, Q.item_id == item_id)
        if "priority" in updates:
            todos.update({"priority": updates["priority"]}, Q.item_id == item_id)
    return {"ok": True, **updates}


@app.post("/analyses/{item_id}/tag")
def tag_item(item_id: str, body: TagRequest, background_tasks: BackgroundTasks):
    """
    Tag an analysis item to a project and trigger background keyword learning.
    Updates the item's project_tag immediately; keyword extraction runs async.
    """
    with db_lock:
        record = analyses.get(Q.item_id == item_id)
    if not record:
        raise HTTPException(status_code=404, detail="Item not found")

    project_name = body.project
    if not any(p.get("name") == project_name for p in config.PROJECTS):
        raise HTTPException(status_code=404, detail="Project not found")

    # Update the stored analysis immediately
    with db_lock:
        analyses.update({"project_tag": project_name}, Q.item_id == item_id)

    def learn() -> None:
        title        = record.get("title", "")
        body_preview = record.get("body_preview", "") or record.get("summary", "")
        keywords     = extract_keywords(project_name, title, body_preview)

        # Collect sender and recipient email addresses from the stored record
        raw_senders: list[str] = []
        for field in (
            record.get("author", ""),
            record.get("to_field", ""),
            record.get("cc_field", ""),
        ):
            raw_senders.extend(extract_emails(field))
        # Exclude the user's own address — it appears on almost every email
        user_addr = (config.USER_EMAIL or "").lower()
        senders = [s for s in raw_senders if s != user_addr]

        if not keywords and not senders:
            return

        with db_lock:
            saved = settings_tbl.get(doc_id=1) or {}

        projects = saved.get("projects", list(config.PROJECTS))
        for p in projects:
            if p.get("name") == project_name:
                if keywords:
                    existing_kw = set(p.get("learned_keywords", []))
                    existing_kw.update(k.lower() for k in keywords)
                    p["learned_keywords"] = list(existing_kw)[:100]
                if senders:
                    existing_sr = set(p.get("learned_senders", []))
                    existing_sr.update(senders)
                    p["learned_senders"] = list(existing_sr)[:50]
                break

        saved["projects"] = projects
        with db_lock:
            if settings_tbl.get(doc_id=1):
                settings_tbl.update(saved, doc_ids=[1])
            else:
                settings_tbl.insert(saved)
        config.apply_overrides(saved)
        kw_total = len(p.get("learned_keywords", []))
        sr_total = len(p.get("learned_senders", []))
        print(f"[learn] {project_name}: +{len(keywords)} keywords ({kw_total} total), "
              f"+{len(senders)} senders ({sr_total} total)")

    background_tasks.add_task(learn)
    return {"ok": True, "project": project_name}


@app.post("/analyses/{item_id}/noise")
def mark_noise(item_id: str, background_tasks: BackgroundTasks):
    """
    Mark an item as irrelevant. Updates its category to noise immediately and
    triggers background keyword extraction to grow the noise filter.
    """
    with db_lock:
        record = analyses.get(Q.item_id == item_id)
    if not record:
        raise HTTPException(status_code=404, detail="Item not found")

    with db_lock:
        analyses.update(
            {"category": "noise", "priority": "low", "has_action": False},
            Q.item_id == item_id,
        )

    def learn_noise() -> None:
        title        = record.get("title", "")
        body_preview = record.get("body_preview", "") or record.get("summary", "")
        keywords     = extract_keywords("noise filter", title, body_preview)
        if not keywords:
            return

        with db_lock:
            saved = settings_tbl.get(doc_id=1) or {}

        existing = set(saved.get("noise_keywords", list(config.NOISE_KEYWORDS)))
        existing.update(k.lower() for k in keywords)
        saved["noise_keywords"] = list(existing)[:200]

        with db_lock:
            if settings_tbl.get(doc_id=1):
                settings_tbl.update(saved, doc_ids=[1])
            else:
                settings_tbl.insert(saved)
        config.apply_overrides(saved)
        print(f"[noise] +{len(keywords)} keywords ({len(existing)} total)")

    background_tasks.add_task(learn_noise)
    return {"ok": True}


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/settings")
def get_settings():
    return {
        "ollama_url":           config.OLLAMA_URL,
        "ollama_model":         config.OLLAMA_MODEL,
        "cf_client_id":         _mask(config.CF_CLIENT_ID),
        "cf_client_secret":     _mask(config.CF_CLIENT_SECRET),
        "slack_client_id":      config.SLACK_CLIENT_ID,
        "slack_client_secret":  _mask(config.SLACK_CLIENT_SECRET),
        "github_pat":           _mask(config.GITHUB_PAT),
        "github_username":      config.GITHUB_USERNAME,
        "jira_email":           config.JIRA_EMAIL,
        "jira_token":           _mask(config.JIRA_TOKEN),
        "jira_domain":          config.JIRA_DOMAIN,
        "jira_jql":             config.JIRA_JQL,
        "lookback_hours":       config.LOOKBACK_HOURS,
        "user_name":            config.USER_NAME,
        "user_email":           config.USER_EMAIL,
        "focus_topics":         ", ".join(config.FOCUS_TOPICS),
        "projects":             config.PROJECTS,
        "noise_keywords":       config.NOISE_KEYWORDS,
        "warnings":             config.validate(),
    }


@app.post("/settings")
def save_settings(body: dict):
    """Persist settings and hot-reload config. Fields containing • are ignored (masked placeholders)."""
    with db_lock:
        existing = settings_tbl.get(doc_id=1) or {}

    for k, v in body.items():
        if v is not None and _MASK not in str(v):
            existing[k] = v

    with db_lock:
        if settings_tbl.get(doc_id=1):
            settings_tbl.update(existing, doc_ids=[1])
        else:
            settings_tbl.insert(existing)

    config.apply_overrides(existing)
    return {"ok": True, "warnings": config.validate()}


# ── Ingest (POST target for host sidecar scripts) ─────────────────────────────

class IngestRequest(BaseModel):
    items: list[dict]


@app.post("/ingest")
def ingest(body: IngestRequest, background_tasks: BackgroundTasks):
    """
    Receive raw items from the Outlook or Thunderbird sidecar scripts.
    Deduplicates by item_id, then queues new items for AI analysis in the background.
    """
    raw: list[RawItem] = []
    for i in body.items:
        iid = i.get("item_id", "")
        if not iid:
            continue
        with db_lock:
            if analyses.get(Q.item_id == iid):
                continue   # already processed
        raw.append(RawItem(
            source    = i.get("source", "outlook"),
            item_id   = iid,
            title     = i.get("title", ""),
            body      = i.get("body", ""),
            url       = i.get("url", ""),
            author    = i.get("author", ""),
            timestamp = i.get("timestamp", now_iso()),
            metadata  = i.get("metadata", {}),
        ))

    def process() -> None:
        with db_lock:
            scan_state["ingest_pending"] += len(raw)
        for item in raw:
            try:
                _save_analysis(analyze(item))
            except Exception as e:
                print(f"[ingest] {item.item_id}: {e}")
            with db_lock:
                scan_state["ingest_pending"] = max(0, scan_state["ingest_pending"] - 1)

    if raw:
        background_tasks.add_task(process)

    return {"received": len(raw), "skipped": len(body.items) - len(raw)}


# ── Scan ──────────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    sources: list[str] = ["slack", "github", "jira", "outlook"]


@app.post("/scan")
def start_scan(body: ScanRequest):
    if scan_state["running"]:
        raise HTTPException(status_code=409, detail="Scan already in progress.")
    threading.Thread(target=_run_scan, args=(body.sources,), daemon=True).start()
    return {"status": "started", "sources": body.sources}


@app.get("/scan/status")
def scan_status():
    return scan_state


@app.post("/scan/cancel")
def cancel_scan():
    """Signal a running scan to stop after the current item finishes."""
    if not scan_state["running"]:
        return {"ok": False, "detail": "No scan running"}
    scan_state["cancelled"] = True
    return {"ok": True}


# ── Todos ─────────────────────────────────────────────────────────────────────

@app.get("/todos")
def get_todos(
    source:   Optional[str] = None,
    priority: Optional[str] = None,
    done:     bool          = False,
):
    with db_lock:
        results = todos.all()

    if not done:
        results = [t for t in results if not t.get("done")]
    if source:
        results = [t for t in results if t.get("source") == source]
    if priority:
        results = [t for t in results if t.get("priority") == priority]

    order = {"high": 0, "medium": 1, "low": 2}
    results.sort(key=lambda t: (order.get(t.get("priority", "low"), 2), t.get("created_at", "")))

    for t in results:
        t["doc_id"] = t.doc_id
        # Back-fill status for records created before this field existed
        if "status" not in t:
            t["status"] = "done" if t.get("done") else "open"

    return results


@app.patch("/todos/{doc_id}")
def patch_todo(doc_id: int, body: dict):
    updates = {}
    # status field takes precedence; "done" bool kept for backward compat
    if "status" in body and body["status"] in ("open", "done", "assigned"):
        updates["status"] = body["status"]
        updates["done"]   = body["status"] == "done"
    elif "done" in body:
        done = bool(body["done"])
        updates["done"]   = done
        updates["status"] = "done" if done else "open"
    if "assigned_to" in body:
        updates["assigned_to"] = body["assigned_to"] or None
    if updates:
        with db_lock:
            todos.update(updates, doc_ids=[doc_id])
    return {"ok": True}


@app.delete("/todos/{doc_id}")
def delete_todo(doc_id: int):
    with db_lock:
        todos.remove(doc_ids=[doc_id])
    return Response(status_code=204)


# ── Analyses ──────────────────────────────────────────────────────────────────

def _deserialize_analysis(a: dict) -> dict:
    """Deserialize JSON-string fields and normalize field names for the frontend."""
    for field in ("action_items", "goals", "key_dates"):
        v = a.get(field)
        if isinstance(v, str):
            try:
                a[field] = json.loads(v)
            except Exception:
                a[field] = []
    # Normalize stored key "urgency" → "urgency_reason"
    if "urgency" in a and "urgency_reason" not in a:
        a["urgency_reason"] = a.pop("urgency")
    return a


@app.get("/analyses")
def get_analyses(
    source:   Optional[str] = None,
    category: Optional[str] = None,
):
    with db_lock:
        results = analyses.all()

    if source:
        results = [a for a in results if a.get("source") == source]
    if category:
        results = [a for a in results if a.get("category") == category]

    results.sort(key=lambda a: a.get("timestamp", ""), reverse=True)
    return [_deserialize_analysis(dict(a)) for a in results[:200]]


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/stats")
def get_stats():
    with db_lock:
        all_a      = analyses.all()
        open_todos = [t for t in todos.all() if not t.get("done")]
        logs       = sorted(
            scan_logs.all(),
            key=lambda l: l.get("finished_at", ""),
            reverse=True,
        )

    by_source: dict[str, int] = {}
    for t in open_todos:
        s = t.get("source", "unknown")
        by_source[s] = by_source.get(s, 0) + 1

    by_category: dict[str, int] = {}
    for a in all_a:
        c = a.get("category", "unknown")
        by_category[c] = by_category.get(c, 0) + 1

    return {
        "total_items":   len(all_a),
        "open_todos":    len(open_todos),
        "high_priority": sum(1 for t in open_todos if t.get("priority") == "high"),
        "by_source":     [{"source": k, "count": v} for k, v in by_source.items()],
        "by_category":   [{"category": k, "count": v} for k, v in by_category.items()],
        "last_scan":     logs[0] if logs else None,
    }


# ── Slack OAuth ────────────────────────────────────────────────────────────────

_SLACK_REDIRECT_URI  = "https://squire.hexcaliper.com/page/api/slack/callback"
_SLACK_USER_SCOPES   = (
    "channels:history,channels:read,groups:history,groups:read,"
    "im:history,im:read,mpim:history,mpim:read,search:read,users:read"
)


@app.get("/slack/connect")
def slack_connect():
    if not config.SLACK_CLIENT_ID:
        raise HTTPException(status_code=400, detail="SLACK_CLIENT_ID not configured — save it in Settings first.")
    url = (
        f"https://slack.com/oauth/v2/authorize"
        f"?client_id={config.SLACK_CLIENT_ID}"
        f"&user_scope={_SLACK_USER_SCOPES}"
        f"&redirect_uri={_SLACK_REDIRECT_URI}"
    )
    return Response(status_code=302, headers={"Location": url})


@app.get("/slack/callback")
def slack_callback(code: str = None, error: str = None):
    if error:
        return Response(status_code=302, headers={"Location": f"/page/?slack_error={error}"})
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code.")

    r = http_requests.post(
        "https://slack.com/api/oauth.v2.access",
        data={
            "client_id":     config.SLACK_CLIENT_ID,
            "client_secret": config.SLACK_CLIENT_SECRET,
            "code":          code,
            "redirect_uri":  _SLACK_REDIRECT_URI,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    if not data.get("ok"):
        return Response(
            status_code=302,
            headers={"Location": f"/page/?slack_error={data.get('error', 'unknown')}"},
        )

    authed_user = data.get("authed_user", {})
    token = authed_user.get("access_token")
    if not token:
        return Response(status_code=302, headers={"Location": "/page/?slack_error=no_user_token"})

    team      = data.get("team", {})
    workspace = {
        "team":    team.get("name", "Unknown"),
        "team_id": team.get("id", ""),
        "token":   token,
    }

    with db_lock:
        existing = settings_tbl.get(doc_id=1) or {}
    tokens = [t for t in existing.get("slack_user_tokens", []) if t.get("team_id") != workspace["team_id"]]
    tokens.append(workspace)
    existing["slack_user_tokens"] = tokens

    with db_lock:
        if settings_tbl.get(doc_id=1):
            settings_tbl.update(existing, doc_ids=[1])
        else:
            settings_tbl.insert(existing)

    config.apply_overrides(existing)
    return Response(status_code=302, headers={"Location": "/page/?slack_connected=1"})


@app.get("/slack/workspaces")
def get_slack_workspaces():
    with db_lock:
        existing = settings_tbl.get(doc_id=1) or {}
    tokens = existing.get("slack_user_tokens", [])
    return [{"team": t.get("team", "Unknown"), "team_id": t.get("team_id", "")} for t in tokens]


@app.delete("/slack/workspaces/{team_id}")
def disconnect_slack_workspace(team_id: str):
    with db_lock:
        existing = settings_tbl.get(doc_id=1) or {}
    tokens = [t for t in existing.get("slack_user_tokens", []) if t.get("team_id") != team_id]
    existing["slack_user_tokens"] = tokens
    with db_lock:
        if settings_tbl.get(doc_id=1):
            settings_tbl.update(existing, doc_ids=[1])
        else:
            settings_tbl.insert(existing)
    config.apply_overrides(existing)
    return {"ok": True}
