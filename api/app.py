"""
app.py — Hexcaliper Squire FastAPI application.

Exposes the REST API consumed by the frontend and the host sidecar scripts.
Key responsibilities:

- Receiving and deduplicating raw items via ``POST /ingest`` (sidecar path).
- Orchestrating multi-source scans via ``POST /scan`` (frontend path).
- Persisting ``Analysis`` and ``Todo`` records to TinyDB.
- Serving settings, stats, and Slack OAuth endpoints to the frontend.

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
from agent import analyze_batch, analyze
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
    "progress":       0,
    "total":          0,
    "current_source": "",
    "current_item":   "",
    "message":        "idle",
    "ingest_pending": 0,
}


def _save_analysis(a: Analysis) -> None:
    """Upsert an Analysis into TinyDB and create todo rows for action items."""
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
    scan_state.update({"running": True, "progress": 0, "total": 0, "message": "Starting..."})
    started   = now_iso()
    all_items: list[RawItem] = []

    for src in sources:
        scan_state.update({"message": f"Fetching {src}...", "current_source": src})
        connector = CONNECTORS.get(src)
        if connector:
            all_items.extend(connector.fetch())

    scan_state["total"]   = len(all_items)
    scan_state["message"] = f"Analyzing {len(all_items)} items..."

    def on_progress(i: int, total: int, source: str, title: str) -> None:
        scan_state.update({
            "progress":       i,
            "current_source": source,
            "current_item":   title,
            "message":        f"[{source}] {i + 1}/{total}: {title}",
        })

    try:
        results = analyze_batch(all_items, progress_cb=on_progress)
        actions = sum(1 for r in results if r.has_action)
        for r in results:
            _save_analysis(r)
        with db_lock:
            scan_logs.insert({
                "started_at":    started,
                "finished_at":   now_iso(),
                "sources":       ",".join(sources),
                "items_scanned": len(all_items),
                "actions_found": actions,
                "status":        "success",
            })
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
                "items_scanned": 0,
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

    return results


@app.patch("/todos/{doc_id}")
def patch_todo(doc_id: int, body: dict):
    updates = {}
    if "done" in body:
        updates["done"] = bool(body["done"])
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
    return results[:200]


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
