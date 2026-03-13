import json
import os
import threading
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from tinydb import TinyDB, Query

import config
from agent import analyze_batch
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

db        = TinyDB(config.DB_PATH)
analyses  = db.table("analyses")
todos     = db.table("todos")
scan_logs = db.table("scan_logs")
Q         = Query()
db_lock   = threading.Lock()

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
    return datetime.utcnow().isoformat()


# ── Scan state ────────────────────────────────────────────────────────────────

scan_state: dict = {
    "running":        False,
    "progress":       0,
    "total":          0,
    "current_source": "",
    "current_item":   "",
    "message":        "idle",
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

@app.get("/health")
def health():
    """Service health check — mirrors hexcaliper's /health response shape."""
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
        for r in analyze_batch(raw):
            _save_analysis(r)

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
