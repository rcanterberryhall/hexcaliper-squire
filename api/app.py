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
import connector_teams
import correlator as _correlator

app = FastAPI(title="Hexcaliper Squire API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)

# Ensure data directory exists before TinyDB opens the file
os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)

db             = TinyDB(config.DB_PATH)
analyses       = db.table("analyses")
todos          = db.table("todos")
scan_logs      = db.table("scan_logs")
settings_tbl   = db.table("settings")
embeddings_tbl = db.table("embeddings")
situations_tbl = db.table("situations")
intel_tbl      = db.table("intel")
Q            = Query()
db_lock      = threading.Lock()
# Limits concurrent Ollama calls — set to 1 for single GPU, raise for multi-GPU
_ollama_sem  = threading.Semaphore(1)

# Single-slot job state for the seed background job (one user, one job at a time)
_seed_job: dict = {"status": "idle"}

# Hot-load any previously saved settings on startup
_saved_settings = settings_tbl.get(doc_id=1)
if _saved_settings:
    config.apply_overrides(_saved_settings)


def _score_decay_loop():
    """Recompute situation scores every 30 minutes to reflect recency decay."""
    import time
    while True:
        time.sleep(1800)
        try:
            _rescore_all_situations()
        except Exception as e:
            print(f"[score_decay] {e}")


threading.Thread(target=_score_decay_loop, daemon=True).start()

CONNECTORS = {
    "slack":   connector_slack,
    "github":  connector_github,
    "jira":    connector_jira,
    "outlook": connector_outlook,
    "teams":   connector_teams,
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


def _save_analysis(a: Analysis, reanalyze: bool = False) -> None:
    """
    Upsert an ``Analysis`` into TinyDB and create todo/intel rows.

    Stores all base fields plus the context-aware enrichment fields:
    ``hierarchy``, ``is_passdown``, ``project_tag``, ``goals`` (JSON),
    ``key_dates`` (JSON), and ``body_preview``.  Todo rows are only inserted
    for action items that do not already exist for the same ``item_id`` and
    ``description`` pair.  Preserves ``situation_id`` and ``references``
    from existing records so the situation layer is not overwritten on re-scan.

    When ``reanalyze=True``, existing todos and intel for this item are
    removed first so stale entries from prior analysis passes do not persist.
    """
    with db_lock:
        existing = analyses.get(Q.item_id == a.item_id)
        existing_situation_id = (existing or {}).get("situation_id")
        if reanalyze:
            todos.remove(Q.item_id == a.item_id)
            intel_tbl.remove(Q.item_id == a.item_id)

        # Extract cross-source references
        refs = _correlator.extract_references(a.title, a.body_preview or "")

        analyses.upsert(
            {
                "item_id":       a.item_id,
                "source":        a.source,
                "title":         a.title,
                "author":        a.author,
                "timestamp":     a.timestamp,
                "url":           a.url,
                "has_action":    a.has_action,
                "priority":      a.priority,
                "category":      a.category,
                "summary":       a.summary,
                "urgency":       a.urgency_reason,
                "action_items":  json.dumps([
                    {"description": x.description, "deadline": x.deadline, "owner": x.owner}
                    for x in a.action_items
                ]),
                "hierarchy":     a.hierarchy,
                "is_passdown":   a.is_passdown,
                "project_tag":   a.project_tag,
                "goals":         json.dumps(a.goals),
                "key_dates":        json.dumps(a.key_dates),
                "information_items": json.dumps(a.information_items),
                "body_preview":     a.body_preview,
                "to_field":      a.to_field,
                "cc_field":      a.cc_field,
                "is_replied":    a.is_replied,
                "replied_at":    a.replied_at,
                "processed_at":  now_iso(),
                "situation_id":  existing_situation_id,
                "references":    json.dumps(refs),
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

        for item in a.information_items:
            if not item.get("fact"):
                continue
            exists = intel_tbl.get(
                (Q.item_id == a.item_id) & (Q.fact == item["fact"])
            )
            if not exists:
                intel_tbl.insert({
                    "item_id":     a.item_id,
                    "source":      a.source,
                    "title":       a.title,
                    "url":         a.url,
                    "fact":        item["fact"],
                    "relevance":   item.get("relevance", ""),
                    "project_tag": a.project_tag,
                    "priority":    a.priority,
                    "timestamp":   a.timestamp,
                    "dismissed":   False,
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
                with _ollama_sem:
                    results.append(analyze(item))
            except Exception as e:
                print(f"[agent] {item.item_id}: {e}")

        actions = sum(1 for r in results if r.has_action)
        for r in results:
            _save_analysis(r)
            threading.Thread(
                target=_maybe_form_situation,
                args=(r.item_id,),
                daemon=True,
            ).start()
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


def _run_reanalyze() -> None:
    """
    Re-run LLM analysis on all stored items using the current config.

    Reconstructs a ``RawItem`` from each stored analysis record (using
    ``body_preview``, ``to_field``, ``cc_field``, and existing ``project_tag``
    as a manual-tag hint), passes it through ``analyze()``, then calls
    ``_save_analysis(reanalyze=True)`` to replace stale todos and intel.
    Situation formation is re-triggered for each item after save.
    Reuses ``scan_state`` for progress reporting.
    """
    scan_state.update({
        "running": True, "cancelled": False,
        "progress": 0, "total": 0, "message": "Loading stored items...",
    })
    started = now_iso()
    try:
        with db_lock:
            all_records = analyses.all()

        # Process passdowns first — they are the richest source of operational
        # current state and their intel should be available when situations
        # form for subsequent items.
        all_records.sort(key=lambda r: (0 if r.get("is_passdown") else 1, r.get("timestamp", "")))

        scan_state["total"]   = len(all_records)
        scan_state["message"] = f"Re-analyzing {len(all_records)} items..."

        results = []
        for i, rec in enumerate(all_records):
            if scan_state["cancelled"]:
                break
            scan_state.update({
                "progress":       i,
                "current_source": rec.get("source", ""),
                "current_item":   (rec.get("title") or "")[:60],
                "message":        f"[{rec.get('source','')}] {i + 1}/{len(all_records)}: {(rec.get('title') or '')[:60]}",
            })
            item = RawItem(
                source    = rec.get("source", ""),
                item_id   = rec.get("item_id", ""),
                title     = rec.get("title", ""),
                body      = rec.get("body_preview", "") or rec.get("title", ""),
                url       = rec.get("url", ""),
                author    = rec.get("author", ""),
                timestamp = rec.get("timestamp", now_iso()),
                metadata  = {
                    "to":          rec.get("to_field", ""),
                    "cc":          rec.get("cc_field", ""),
                    "is_replied":  rec.get("is_replied", False),
                    "replied_at":  rec.get("replied_at"),
                    "project_tag": rec.get("project_tag"),
                    "hierarchy":   rec.get("hierarchy"),
                },
            )
            try:
                with _ollama_sem:
                    result = analyze(item)
                results.append(result)
            except Exception as e:
                print(f"[reanalyze] {item.item_id}: {e}")

        actions = sum(1 for r in results if r.has_action)
        for r in results:
            _save_analysis(r, reanalyze=True)
            threading.Thread(
                target=_maybe_form_situation,
                args=(r.item_id,),
                daemon=True,
            ).start()

        status = "cancelled" if scan_state["cancelled"] else "success"
        with db_lock:
            scan_logs.insert({
                "started_at":    started,
                "finished_at":   now_iso(),
                "sources":       "reanalyze",
                "items_scanned": len(results),
                "actions_found": actions,
                "status":        status,
            })
        scan_state["message"] = (
            f"Re-analysis complete — {len(results)} items processed, "
            f"{actions} action items found."
        )
    except Exception as e:
        scan_state["message"] = f"Re-analysis error: {e}"
        print(f"[reanalyze] {e}")
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
    """Drop all analyses, todos, scan logs, and embeddings. Settings are preserved."""
    with db_lock:
        analyses.truncate()
        todos.truncate()
        intel_tbl.truncate()
        scan_logs.truncate()
        embeddings_tbl.truncate()
        situations_tbl.truncate()
    return {"ok": True}


@app.get("/projects")
def get_projects():
    """Return configured projects with learned keyword, sender, and embedding counts."""
    from embedder import get_project_stats
    stats = get_project_stats()
    return [
        {
            "name":               p.get("name", ""),
            "keywords":           p.get("keywords", []),
            "channels":           p.get("channels", []),
            "learned_keywords":   p.get("learned_keywords", []),
            "learned_count":      len(p.get("learned_keywords", [])),
            "learned_senders":    p.get("learned_senders", []),
            "sender_count":       len(p.get("learned_senders", [])),
            "embedding_items":    stats.get(p.get("name", ""), {}).get("total_items", 0),
            "embedding_subs":     stats.get(p.get("name", ""), {}).get("subdivisions", []),
        }
        for p in config.PROJECTS
    ]


class TagRequest(BaseModel):
    project: str


@app.patch("/analyses/{item_id}")
def patch_analysis(item_id: str, body: dict, background_tasks: BackgroundTasks):
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
    if "project_tag" in body:
        updates["project_tag"] = body["project_tag"] or None
    if "is_passdown" in body and isinstance(body["is_passdown"], bool):
        updates["is_passdown"] = body["is_passdown"]
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update.")
    with db_lock:
        old_record = analyses.get(Q.item_id == item_id)
        if not old_record:
            raise HTTPException(status_code=404, detail="Item not found")
        analyses.update(updates, Q.item_id == item_id)
        if updates.get("category") == "noise":
            todos.remove(Q.item_id == item_id)
        elif "priority" in updates:
            todos.update({"priority": updates["priority"]}, Q.item_id == item_id)

    old_project  = old_record.get("project_tag")
    old_category = old_record.get("category")
    new_project  = updates.get("project_tag", old_project)
    new_category = updates.get("category", old_category)
    project_changed  = "project_tag" in updates and new_project != old_project
    category_changed = "category" in updates and new_category != old_category

    if (project_changed or category_changed) and (new_project or old_project):
        def relearn() -> None:
            with db_lock:
                record = analyses.get(Q.item_id == item_id)
            if not record:
                return
            body_text = record.get("body_preview", "") or record.get("summary", "")
            if not body_text:
                return
            try:
                from embedder import embed, update_project, remove_item
                vector = embed(body_text)
                if new_project:
                    update_project(
                        project_name = new_project,
                        item_id      = item_id,
                        vector       = vector,
                        category     = new_category,
                        hierarchy    = record.get("hierarchy", "general"),
                        source       = record.get("source", ""),
                        priority     = record.get("priority", "medium"),
                        old_project  = old_project if project_changed else None,
                        old_category = old_category if category_changed else None,
                    )
                elif old_project:
                    remove_item(item_id, old_project)
            except Exception as e:
                print(f"[patch] embedding update failed: {e}")
        background_tasks.add_task(relearn)

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

        # Embedding update
        body_text = record.get("body_preview", "") or record.get("summary", "")
        if body_text:
            try:
                from embedder import embed, update_project
                vector = embed(body_text)
                update_project(
                    project_name = project_name,
                    item_id      = item_id,
                    vector       = vector,
                    category     = record.get("category", "fyi"),
                    hierarchy    = record.get("hierarchy", "general"),
                    source       = record.get("source", ""),
                    priority     = record.get("priority", "medium"),
                    old_project  = None,
                    old_category = None,
                )
            except Exception as e:
                print(f"[learn] embedding update failed: {e}")

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
        todos.remove(Q.item_id == item_id)

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
                with _ollama_sem:
                    result = analyze(item)
                _save_analysis(result)
                threading.Thread(
                    target=_maybe_form_situation,
                    args=(result.item_id,),
                    daemon=True,
                ).start()
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


@app.post("/reanalyze")
def start_reanalyze():
    """Re-run LLM analysis on all stored items with the current config."""
    if scan_state["running"]:
        raise HTTPException(status_code=409, detail="A scan or re-analysis is already running.")
    with db_lock:
        count = len(analyses.all())
    threading.Thread(target=_run_reanalyze, daemon=True).start()
    return {"status": "started", "item_count": count}


@app.get("/reanalyze/count")
def reanalyze_count():
    """Return the number of stored items that would be re-analyzed."""
    with db_lock:
        return {"count": len(analyses.all())}


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


# ── Intel ──────────────────────────────────────────────────────────────────────

@app.get("/intel")
def get_intel(
    source:  Optional[str] = None,
    project: Optional[str] = None,
):
    """Return undismissed intel items sorted by timestamp descending."""
    with db_lock:
        results = intel_tbl.all()
    results = [r for r in results if not r.get("dismissed")]
    if source:
        results = [r for r in results if r.get("source") == source]
    if project:
        results = [r for r in results if r.get("project_tag") == project]
    results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    for r in results:
        r["doc_id"] = r.doc_id
    return results


@app.delete("/intel/{doc_id}")
def delete_intel(doc_id: int):
    with db_lock:
        intel_tbl.remove(doc_ids=[doc_id])
    return Response(status_code=204)


@app.patch("/intel/{doc_id}")
def patch_intel(doc_id: int, body: dict):
    if "dismissed" in body:
        with db_lock:
            intel_tbl.update({"dismissed": bool(body["dismissed"])}, doc_ids=[doc_id])
    return {"ok": True}


# ── Analyses ──────────────────────────────────────────────────────────────────

def _deserialize_analysis(a: dict) -> dict:
    """Deserialize JSON-string fields and normalize field names for the frontend."""
    for field in ("action_items", "goals", "key_dates", "information_items"):
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
    source:    Optional[str] = None,
    category:  Optional[str] = None,
    hierarchy: Optional[str] = None,
    project:   Optional[str] = None,
    q:         Optional[str] = None,
    from_date: Optional[str] = None,
    to_date:   Optional[str] = None,
    limit:     int = 1000,
):
    with db_lock:
        results = analyses.all()

    if source:
        results = [a for a in results if a.get("source") == source]
    if category:
        results = [a for a in results if a.get("category") == category]
    if hierarchy:
        results = [a for a in results if a.get("hierarchy") == hierarchy]
    if project == "__none__":
        results = [a for a in results if not a.get("project_tag")]
    elif project:
        results = [a for a in results if a.get("project_tag") == project]
    if q:
        ql = q.lower()
        results = [a for a in results if any(
            ql in (a.get(f) or "").lower()
            for f in ("title", "summary", "author", "body_preview")
        )]
    if from_date:
        results = [a for a in results if (a.get("timestamp") or "") >= from_date]
    if to_date:
        results = [a for a in results if (a.get("timestamp") or "") <= to_date]

    results.sort(key=lambda a: a.get("timestamp", ""), reverse=True)
    return [_deserialize_analysis(dict(a)) for a in results[:limit]]


# ── Situation layer ───────────────────────────────────────────────────────────

import uuid as _uuid


def _pri_rank(p: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(p, 1)


def _maybe_form_situation(item_id: str) -> None:
    """
    Check whether a newly saved item correlates with existing analyses and
    create or update a Situation record accordingly.
    """
    try:
        from embedder import get_item_vector

        with db_lock:
            record       = analyses.get(Q.item_id == item_id)
            all_analyses = analyses.all()
        if not record:
            return

        raw_refs = record.get("references")
        try:
            refs = json.loads(raw_refs) if isinstance(raw_refs, str) else (raw_refs or [])
        except Exception:
            refs = []

        vector     = get_item_vector(item_id)
        project    = record.get("project_tag")
        candidates = _correlator.find_correlated_candidates(item_id, refs, vector or [], project, all_analyses)

        if not candidates:
            # No new correlations — but rescore existing situation if item is already in one
            existing_sit_id = record.get("situation_id")
            if existing_sit_id:
                _rescore_situation(existing_sit_id)
            return

        # Check whether any candidate already belongs to a situation
        with db_lock:
            sit_ids = set()
            for cid in candidates:
                r = analyses.get(Q.item_id == cid)
                if r and r.get("situation_id"):
                    sit_ids.add(r["situation_id"])

        if sit_ids:
            # Merge into the highest-scoring existing situation
            target_sit_id = sit_ids.pop()
            with db_lock:
                sit = situations_tbl.get(Q.situation_id == target_sit_id)
            if sit:
                updated_ids = list(dict.fromkeys(sit.get("item_ids", []) + [item_id]))
                # Merge any additional sit_ids
                for extra_sid in sit_ids:
                    with db_lock:
                        extra = situations_tbl.get(Q.situation_id == extra_sid)
                    if extra:
                        updated_ids = list(dict.fromkeys(updated_ids + extra.get("item_ids", [])))
                        with db_lock:
                            situations_tbl.remove(Q.situation_id == extra_sid)

                _update_situation_record(target_sit_id, updated_ids)
                with db_lock:
                    analyses.update({"situation_id": target_sit_id}, Q.item_id == item_id)
                return

        # No existing situation — check minimum cluster requirements
        all_ids = [item_id] + candidates
        with db_lock:
            cluster_records = [analyses.get(Q.item_id == iid) for iid in all_ids]
        cluster_records = [r for r in cluster_records if r]

        if len(cluster_records) < 2:
            return

        # Create new situation
        with db_lock:
            cluster_intel = [
                i for i in intel_tbl.all()
                if i.get("item_id") in set(all_ids) and not i.get("dismissed")
            ]
        synthesis   = _correlator.synthesize_situation(cluster_records, config.USER_NAME or "the user", intel_items=cluster_intel)
        score       = _correlator.score_situation(all_ids, cluster_records)
        max_pri     = max(cluster_records, key=lambda r: _pri_rank(r.get("priority", "low")))
        all_refs    = list(set(r for rec in cluster_records
                               for raw in [rec.get("references")]
                               for r in (json.loads(raw) if isinstance(raw, str) and raw else (raw or []))))
        project_tags = list(set(r.get("project_tag") for r in cluster_records if r.get("project_tag")))
        sources      = list(set(r.get("source", "") for r in cluster_records))
        last_ts      = max((r.get("timestamp", "") for r in cluster_records), default=now_iso())

        sit_id  = str(_uuid.uuid4())
        sit_doc = {
            "situation_id":     sit_id,
            "title":            synthesis.get("title", "Correlated situation"),
            "summary":          synthesis.get("summary", ""),
            "status":           synthesis.get("status", "in_progress"),
            "item_ids":         all_ids,
            "sources":          sources,
            "project_tag":      project_tags[0] if len(project_tags) == 1 else None,
            "score":            score,
            "priority":         max_pri.get("priority", "medium"),
            "open_actions":     synthesis.get("open_actions", []),
            "references":       all_refs,
            "key_context":      synthesis.get("key_context"),
            "last_updated":     last_ts,
            "created_at":       now_iso(),
            "score_updated_at": now_iso(),
            "dismissed":        False,
        }
        with db_lock:
            situations_tbl.insert(sit_doc)
            for iid in all_ids:
                analyses.update({"situation_id": sit_id}, Q.item_id == iid)

        print(f"[correlator] formed situation {sit_id[:8]} from {len(all_ids)} items")

    except Exception as e:
        print(f"[correlator] _maybe_form_situation({item_id}): {e}")


def _update_situation_record(sit_id: str, item_ids: list) -> None:
    """Reload cluster records and recompute score + synthesis for an existing situation."""
    try:
        with db_lock:
            cluster_records = [analyses.get(Q.item_id == iid) for iid in item_ids]
        cluster_records = [r for r in cluster_records if r]
        if not cluster_records:
            return

        with db_lock:
            cluster_intel = [
                i for i in intel_tbl.all()
                if i.get("item_id") in set(item_ids) and not i.get("dismissed")
            ]
        synthesis   = _correlator.synthesize_situation(cluster_records, config.USER_NAME or "the user", intel_items=cluster_intel)
        score       = _correlator.score_situation(item_ids, cluster_records)
        max_pri     = max(cluster_records, key=lambda r: _pri_rank(r.get("priority", "low")))
        all_refs    = list(set(r for rec in cluster_records
                               for raw in [rec.get("references")]
                               for r in (json.loads(raw) if isinstance(raw, str) and raw else (raw or []))))
        sources     = list(set(r.get("source", "") for r in cluster_records))
        last_ts     = max((r.get("timestamp", "") for r in cluster_records), default=now_iso())
        proj_tags   = list(set(r.get("project_tag") for r in cluster_records if r.get("project_tag")))

        updates = {
            "item_ids":         item_ids,
            "sources":          sources,
            "score":            score,
            "priority":         max_pri.get("priority", "medium"),
            "title":            synthesis.get("title") or "",
            "summary":          synthesis.get("summary") or "",
            "status":           synthesis.get("status") or "in_progress",
            "open_actions":     synthesis.get("open_actions") or [],
            "key_context":      synthesis.get("key_context"),
            "references":       all_refs,
            "last_updated":     last_ts,
            "project_tag":      proj_tags[0] if len(proj_tags) == 1 else None,
            "score_updated_at": now_iso(),
        }
        with db_lock:
            situations_tbl.update(updates, Q.situation_id == sit_id)
            for iid in item_ids:
                analyses.update({"situation_id": sit_id}, Q.item_id == iid)
    except Exception as e:
        print(f"[correlator] _update_situation_record({sit_id}): {e}")


def _rescore_situation(sit_id: str) -> None:
    """Recompute only the score (not synthesis) for a single situation."""
    try:
        with db_lock:
            sit = situations_tbl.get(Q.situation_id == sit_id)
        if not sit:
            return
        item_ids = sit.get("item_ids", [])
        with db_lock:
            cluster_records = [analyses.get(Q.item_id == iid) for iid in item_ids]
        cluster_records = [r for r in cluster_records if r]
        if not cluster_records:
            return
        score = _correlator.score_situation(item_ids, cluster_records)
        with db_lock:
            situations_tbl.update(
                {"score": score, "score_updated_at": now_iso()},
                Q.situation_id == sit_id,
            )
    except Exception as e:
        print(f"[correlator] _rescore_situation({sit_id}): {e}")


def _rescore_all_situations() -> None:
    """Recompute scores for all stored situations (called by decay loop)."""
    with db_lock:
        sit_ids = [s["situation_id"] for s in situations_tbl.all() if not s.get("dismissed")]
    for sid in sit_ids:
        try:
            _rescore_situation(sid)
        except Exception as e:
            print(f"[score_decay] {sid}: {e}")


def _situation_response(sit: dict) -> dict:
    """Build the API response dict for a situation, including lightweight item summaries."""
    item_ids = sit.get("item_ids", [])
    with db_lock:
        items_raw = [analyses.get(Q.item_id == iid) for iid in item_ids]
    items = [
        {
            "item_id":   r.get("item_id"),
            "source":    r.get("source"),
            "title":     r.get("title"),
            "priority":  r.get("priority"),
            "timestamp": r.get("timestamp"),
        }
        for r in items_raw if r
    ]
    return {
        "situation_id": sit.get("situation_id"),
        "title":        sit.get("title"),
        "summary":      sit.get("summary"),
        "status":       sit.get("status"),
        "score":        sit.get("score", 0.0),
        "priority":     sit.get("priority"),
        "sources":      sit.get("sources", []),
        "project_tag":  sit.get("project_tag"),
        "open_actions": sit.get("open_actions", []),
        "references":   sit.get("references", []),
        "key_context":  sit.get("key_context"),
        "last_updated": sit.get("last_updated"),
        "created_at":   sit.get("created_at"),
        "dismissed":    sit.get("dismissed", False),
        "item_count":   len(items),
        "items":        items,
    }


@app.get("/situations")
def get_situations(
    project:   Optional[str] = None,
    status:    Optional[str] = None,
    min_score: float         = 0.0,
):
    """Return all active situations sorted by score descending."""
    with db_lock:
        all_sits = situations_tbl.all()
    results = [s for s in all_sits if not s.get("dismissed")]
    if project:
        results = [s for s in results if s.get("project_tag") == project]
    if status:
        results = [s for s in results if s.get("status") == status]
    if min_score:
        results = [s for s in results if s.get("score", 0) >= min_score]
    results.sort(key=lambda s: s.get("score", 0), reverse=True)
    return [_situation_response(s) for s in results]


@app.get("/situations/{situation_id}")
def get_situation(situation_id: str):
    """Return a single situation with full contributing analyses deserialized."""
    with db_lock:
        sit = situations_tbl.get(Q.situation_id == situation_id)
    if not sit:
        raise HTTPException(status_code=404, detail="Situation not found")
    resp = _situation_response(sit)
    # Replace lightweight items with fully deserialized analyses
    item_ids = sit.get("item_ids", [])
    with db_lock:
        full_items = [analyses.get(Q.item_id == iid) for iid in item_ids]
    resp["items"] = [_deserialize_analysis(dict(r)) for r in full_items if r]
    return resp


@app.post("/situations/{situation_id}/dismiss")
def dismiss_situation(situation_id: str, body: dict = {}):
    """Mark a situation as dismissed; excluded from GET /situations by default."""
    with db_lock:
        if not situations_tbl.get(Q.situation_id == situation_id):
            raise HTTPException(status_code=404, detail="Situation not found")
        situations_tbl.update(
            {"dismissed": True, "dismiss_reason": body.get("reason")},
            Q.situation_id == situation_id,
        )
    return {"ok": True}


@app.post("/situations/{situation_id}/rescore")
def rescore_situation(situation_id: str):
    """Manually trigger score recomputation and LLM re-synthesis."""
    with db_lock:
        sit = situations_tbl.get(Q.situation_id == situation_id)
    if not sit:
        raise HTTPException(status_code=404, detail="Situation not found")
    _update_situation_record(situation_id, sit.get("item_ids", []))
    with db_lock:
        updated = situations_tbl.get(Q.situation_id == situation_id)
    return _situation_response(updated)


@app.patch("/situations/{situation_id}")
def patch_situation(situation_id: str, body: dict):
    """Allow manual override of title, status, and project_tag."""
    allowed = {"title", "status", "project_tag"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update.")
    with db_lock:
        if not situations_tbl.get(Q.situation_id == situation_id):
            raise HTTPException(status_code=404, detail="Situation not found")
        situations_tbl.update(updates, Q.situation_id == situation_id)
    return {"ok": True, **updates}


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

    with db_lock:
        all_sits   = situations_tbl.all()
        open_intel = [i for i in intel_tbl.all() if not i.get("dismissed")]

    return {
        "total_items":           len(all_a),
        "open_todos":            len(open_todos),
        "high_priority":         sum(1 for t in open_todos if t.get("priority") == "high"),
        "open_intel":            len(open_intel),
        "by_source":             [{"source": k, "count": v} for k, v in by_source.items()],
        "by_category":           [{"category": k, "count": v} for k, v in by_category.items()],
        "last_scan":             logs[0] if logs else None,
        "open_situations":       len([s for s in all_sits if not s.get("dismissed")]),
        "high_score_situations": len([s for s in all_sits
                                      if not s.get("dismissed") and s.get("score", 0) >= 1.5]),
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


# ── Teams OAuth ────────────────────────────────────────────────────────────────

_TEAMS_REDIRECT_URI = "https://squire.hexcaliper.com/page/api/teams/callback"
_TEAMS_SCOPES       = "Chat.Read ChannelMessage.Read.All Channel.ReadBasic.All offline_access"


@app.get("/teams/connect")
def teams_connect():
    if not config.TEAMS_CLIENT_ID:
        raise HTTPException(status_code=400, detail="TEAMS_CLIENT_ID not configured — save it in Settings first.")
    url = (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        f"?client_id={config.TEAMS_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={_TEAMS_REDIRECT_URI}"
        f"&scope={_TEAMS_SCOPES}"
        f"&response_mode=query"
    )
    return Response(status_code=302, headers={"Location": url})


@app.get("/teams/callback")
def teams_callback(code: str = None, error: str = None, error_description: str = None):
    if error:
        return Response(status_code=302, headers={"Location": f"/page/?teams_error={error}"})
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code.")

    r = http_requests.post(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  _TEAMS_REDIRECT_URI,
            "client_id":     config.TEAMS_CLIENT_ID,
            "client_secret": config.TEAMS_CLIENT_SECRET,
            "scope":         _TEAMS_SCOPES,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    if "error" in data:
        return Response(
            status_code=302,
            headers={"Location": f"/page/?teams_error={data.get('error', 'unknown')}"},
        )

    access_token  = data.get("access_token")
    refresh_token = data.get("refresh_token")
    if not access_token:
        return Response(status_code=302, headers={"Location": "/page/?teams_error=no_access_token"})

    # Resolve display name from Graph /me
    try:
        me_r = http_requests.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        me_r.raise_for_status()
        me = me_r.json()
        display_name = me.get("displayName", "Unknown")
        account_id   = me.get("id", "")
        tenant       = me.get("userPrincipalName", "").split("@")[-1] or "teams"
    except Exception:
        display_name = "Unknown"
        account_id   = ""
        tenant       = "teams"

    account = {
        "display_name":  display_name,
        "account_id":    account_id,
        "tenant":        tenant,
        "access_token":  access_token,
        "refresh_token": refresh_token or "",
    }

    with db_lock:
        existing = settings_tbl.get(doc_id=1) or {}
    tokens = [t for t in existing.get("teams_user_tokens", []) if t.get("account_id") != account_id]
    tokens.append(account)
    existing["teams_user_tokens"] = tokens

    with db_lock:
        if settings_tbl.get(doc_id=1):
            settings_tbl.update(existing, doc_ids=[1])
        else:
            settings_tbl.insert(existing)

    config.apply_overrides(existing)
    return Response(status_code=302, headers={"Location": "/page/?teams_connected=1"})


@app.get("/teams/workspaces")
def get_teams_workspaces():
    with db_lock:
        existing = settings_tbl.get(doc_id=1) or {}
    tokens = existing.get("teams_user_tokens", [])
    return [
        {"display_name": t.get("display_name", "Unknown"), "account_id": t.get("account_id", ""), "tenant": t.get("tenant", "")}
        for t in tokens
    ]


@app.delete("/teams/workspaces/{account_id}")
def disconnect_teams_account(account_id: str):
    with db_lock:
        existing = settings_tbl.get(doc_id=1) or {}
    tokens = [t for t in existing.get("teams_user_tokens", []) if t.get("account_id") != account_id]
    existing["teams_user_tokens"] = tokens
    with db_lock:
        if settings_tbl.get(doc_id=1):
            settings_tbl.update(existing, doc_ids=[1])
        else:
            settings_tbl.insert(existing)
    config.apply_overrides(existing)
    return {"ok": True}


# ── Seed endpoints ─────────────────────────────────────────────────────────────

MAP_PROMPT = """\
You are analyzing work items from {user_name}'s ops inbox.
{context_block}

Identify distinct ongoing projects or workstreams and recurring operational concerns from these items.
Passdown notes describe active operational handoffs — weight them heavily.

Items:
{items_block}

Respond ONLY with valid JSON — no markdown, no explanation:
{{
  "projects": [
    {{"name": "short project name", "keywords": ["keyword1", "keyword2", "keyword3"]}}
  ],
  "concerns": ["brief recurring concern phrase"]
}}
"""

REDUCE_PROMPT = """\
You are synthesizing project intelligence for {user_name}.
{context_block}

Below are theme extracts from {n_batches} batches covering {n_items} work items.

{themes_block}

Produce a final consolidated list. Merge similar projects. Keep only projects with strong evidence. Topics are recurring concerns that don't fit a specific project.

Respond ONLY with valid JSON — no markdown, no explanation:
{{
  "projects": [
    {{"name": "canonical project name", "keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"]}}
  ],
  "topics": ["watch topic or recurring concern phrase"]
}}
"""


def _run_seed_job(context: str) -> None:
    """
    Background thread for POST /seed.  Implements the full bootstrap state machine:

    waiting_for_ingest → analyzing → review (thread exits; user applies)

    After apply, seed_apply() transitions to reanalyzing → scan_prompt → done.
    """
    import time
    global _seed_job
    try:
        user_name = config.USER_NAME or "the user"

        # ── State: waiting_for_ingest ─────────────────────────────────────────
        # Poll until at least one item has been ingested and the ingest queue
        # has drained.  Context can be updated via PATCH /seed/context while
        # waiting, so we read it from _seed_job just before analysis starts.
        seen_items = False
        while True:
            with db_lock:
                item_count = len(analyses.all())
            pending = scan_state.get("ingest_pending", 0)
            if item_count > 0:
                seen_items = True
            _seed_job.update({
                "state":          "waiting_for_ingest",
                "item_count":     item_count,
                "ingest_pending": pending,
                "progress":       (
                    f"{item_count} item{'s' if item_count != 1 else ''} received"
                    + (f", {pending} processing…" if pending else
                       " — ingest complete" if seen_items else "")
                ),
            })
            if seen_items and pending == 0:
                break
            if _seed_job.get("cancelled"):
                _seed_job.update({"state": "idle", "status": "idle"})
                return
            time.sleep(3)

        # ── State: analyzing ──────────────────────────────────────────────────
        # Re-read context now in case it was updated while waiting.
        context = _seed_job.get("context") or context
        context_block = f"Context about {user_name}: {context}" if context else ""
        _seed_job.update({"state": "analyzing", "progress": "Starting analysis…"})

        # 1. Fetch all analyses, sort passdowns first then by priority, cap at 120
        with db_lock:
            all_items = analyses.all()

        priority_rank = {"high": 3, "medium": 2, "low": 1}

        def _sort_key(a):
            is_pd = 1 if a.get("is_passdown") else 0
            pri   = priority_rank.get(a.get("priority", "low"), 1)
            return (is_pd, pri)

        all_items.sort(key=_sort_key, reverse=True)
        all_items = all_items[:120]

        passdown_count = sum(1 for a in all_items if a.get("is_passdown"))
        n_items        = len(all_items)

        batch_size = 6
        n_batches  = max(1, (n_items + batch_size - 1) // batch_size)

        _seed_job["progress"] = f"Analysing {n_items} items ({passdown_count} passdowns)…"

        # 2. Map pass
        map_results  = []
        last_map_err = None
        for batch_num, batch_start in enumerate(range(0, n_items, batch_size), 1):
            _seed_job["progress"] = f"Map pass: batch {batch_num}/{n_batches}…"
            batch = all_items[batch_start:batch_start + batch_size]
            lines = []
            for a in batch:
                source   = a.get("source", "?")
                title    = a.get("title", "(no title)")
                # Truncate summary to keep prompt small
                summary  = (a.get("summary", "") or "")[:120]
                priority = a.get("priority", "low")
                is_pd    = a.get("is_passdown", False)
                suffix   = " [passdown]" if is_pd else ""
                lines.append(f"[{source}]{suffix} {title}: {summary} ({priority})")
            items_block = "\n".join(lines)

            prompt = MAP_PROMPT.format(
                user_name=user_name,
                context_block=context_block,
                items_block=items_block,
            )
            try:
                with _ollama_sem:
                    resp = http_requests.post(
                        config.OLLAMA_URL,
                        headers=config.ollama_headers(),
                        json={
                            "model":   config.OLLAMA_MODEL,
                            "prompt":  prompt,
                            "stream":  False,
                            "format":  "json",
                            "options": {"temperature": 0.2, "num_predict": 300},
                        },
                        timeout=120,
                    )
                resp.raise_for_status()
                data = json.loads(resp.json().get("response", "{}"))
                map_results.append(data)
            except Exception as e:
                last_map_err = str(e)
                print(f"[seed] map batch {batch_start} failed: {e}")
                continue

        if not map_results:
            err_detail = f" ({last_map_err})" if last_map_err else ""
            _seed_job.update({
                "state":          "error",
                "status":         "error",
                "progress":       f"All map batches failed{err_detail}",
                "projects":       [],
                "topics":         [],
                "item_count":     n_items,
                "passdown_count": passdown_count,
            })
            return

        # 3. Reduce pass
        _seed_job["progress"] = f"Reduce pass: consolidating {len(map_results)} batches…"
        themes_parts = []
        for i, mr in enumerate(map_results):
            projects_str = json.dumps(mr.get("projects", []))
            concerns_str = json.dumps(mr.get("concerns", []))
            themes_parts.append(f"Batch {i + 1}:\n  projects: {projects_str}\n  concerns: {concerns_str}")
        themes_block = "\n\n".join(themes_parts)

        reduce_prompt = REDUCE_PROMPT.format(
            user_name=user_name,
            context_block=context_block,
            n_batches=len(map_results),
            n_items=n_items,
            themes_block=themes_block,
        )

        try:
            with _ollama_sem:
                resp = http_requests.post(
                    config.OLLAMA_URL,
                    headers=config.ollama_headers(),
                    json={
                        "model":   config.OLLAMA_MODEL,
                        "prompt":  reduce_prompt,
                        "stream":  False,
                        "format":  "json",
                        "options": {"temperature": 0.2, "num_predict": 400},
                    },
                    timeout=120,
                )
            resp.raise_for_status()
            final    = json.loads(resp.json().get("response", "{}"))
            projects = final.get("projects", [])
            topics   = final.get("topics", [])
        except Exception as e:
            print(f"[seed] reduce failed: {e}")
            # Fallback: flat merge of map results
            projects   = []
            seen_names = set()
            topics     = []
            for mr in map_results:
                for p in mr.get("projects", []):
                    nm = p.get("name", "")
                    if nm and nm not in seen_names:
                        seen_names.add(nm)
                        projects.append(p)
                topics.extend(mr.get("concerns", []))
            topics = list(dict.fromkeys(topics))

        _seed_job.update({
            "state":          "review",
            "status":         "running",
            "progress":       f"Analysis complete — {n_items} items reviewed.",
            "projects":       projects,
            "topics":         topics,
            "item_count":     n_items,
            "passdown_count": passdown_count,
        })
        # Thread exits here.  Frontend reads state="review" and shows the
        # project/topic editor.  POST /seed/apply continues the state machine.

    except Exception as e:
        _seed_job.update({"state": "error", "status": "error", "progress": str(e)})


@app.post("/seed")
async def seed_preview(request: Request):
    """
    Start the seed state machine.  Always succeeds immediately — the
    ``waiting_for_ingest`` phase handles empty databases by polling until
    items arrive.  Returns the current ``_seed_job`` state.
    """
    global _seed_job
    active_states = {"waiting_for_ingest", "analyzing", "reanalyzing", "scanning"}
    if _seed_job.get("state") in active_states:
        return _seed_job

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    context = body.get("context", "") if isinstance(body, dict) else ""

    _seed_job = {
        "state":    "waiting_for_ingest",
        "status":   "running",
        "progress": "Waiting for ingest…",
        "context":  context,
        "item_count":     0,
        "ingest_pending": 0,
    }
    threading.Thread(target=_run_seed_job, args=(context,), daemon=True).start()
    return _seed_job


@app.patch("/seed/context")
async def seed_update_context(request: Request):
    """Update the context string while waiting for ingest."""
    body = await request.json()
    _seed_job["context"] = body.get("context", "")
    return {"ok": True}


@app.get("/seed/status")
def seed_status():
    """Return the current state of the background seed job."""
    return _seed_job


@app.post("/seed/apply")
def seed_apply(body: dict, background_tasks: BackgroundTasks):
    """
    Apply seeded projects and topics to settings, optionally re-tagging existing
    analyses.  After retagging, runs embeddings for all newly-tagged items in the
    background to populate centroids, then sweeps situation formation across the
    full corpus so the correlation layer starts with a warm model.
    """
    suggested_projects = body.get("projects", [])
    suggested_topics   = body.get("topics", [])
    retag              = body.get("retag", True)

    # 1. Load existing settings
    with db_lock:
        existing = settings_tbl.get(doc_id=1) or {}

    current_projects: list[dict] = existing.get("projects", list(config.PROJECTS))
    current_names = {p.get("name", "").lower() for p in current_projects}

    # 2. Merge projects
    projects_added = 0
    for sp in suggested_projects:
        name = sp.get("name", "").strip()
        if not name:
            continue
        if name.lower() not in current_names:
            current_projects.append({
                "name":             name,
                "keywords":         sp.get("keywords", []),
                "channels":         [],
                "learned_keywords": [],
                "learned_senders":  [],
            })
            current_names.add(name.lower())
            projects_added += 1

    # 3. Merge topics into focus_topics (comma-separated, deduplicated)
    existing_ft_raw = existing.get("focus_topics", ", ".join(config.FOCUS_TOPICS))
    existing_topics = [t.strip() for t in existing_ft_raw.split(",") if t.strip()]
    existing_topics_lower = {t.lower() for t in existing_topics}
    topics_added = 0
    for t in suggested_topics:
        t = t.strip()
        if t and t.lower() not in existing_topics_lower:
            existing_topics.append(t)
            existing_topics_lower.add(t.lower())
            topics_added += 1
    new_focus_topics = ", ".join(existing_topics)

    # 4. Save back
    existing["projects"]     = current_projects
    existing["focus_topics"] = new_focus_topics
    with db_lock:
        if settings_tbl.get(doc_id=1):
            settings_tbl.update(existing, doc_ids=[1])
        else:
            settings_tbl.insert(existing)
    config.apply_overrides(existing)

    # 5. Optionally retag existing analyses
    items_retagged = 0
    if retag and projects_added > 0:
        # Only consider newly-added projects for retagging
        new_projects = current_projects[-projects_added:]
        with db_lock:
            all_items = analyses.all()

        updates = []
        for item in all_items:
            if item.get("project_tag"):
                continue
            text = " ".join([
                item.get("title", ""),
                item.get("body_preview", ""),
                item.get("summary", ""),
            ]).lower()
            for proj in new_projects:
                kws = proj.get("keywords", []) + proj.get("learned_keywords", [])
                if any(kw.lower() in text for kw in kws if kw):
                    updates.append((item.doc_id, proj["name"]))
                    items_retagged += 1
                    break

        if updates:
            with db_lock:
                for doc_id, tag in updates:
                    analyses.update({"project_tag": tag}, doc_ids=[doc_id])

    # 6. Background: embed all newly-tagged items, then sweep situation formation
    #    across the full corpus.  This is the step that populates centroids and
    #    warms the correlation layer from a bulk corpus instead of a trickle.
    def _seed_embed_and_correlate() -> None:
        print("[seed] starting embedding sweep...")
        try:
            from embedder import embed, update_project
            with db_lock:
                all_items = analyses.all()
            for item in all_items:
                tag = item.get("project_tag")
                if not tag:
                    continue
                text = " ".join(filter(None, [
                    item.get("title", ""),
                    item.get("body_preview", ""),
                    item.get("summary", ""),
                ]))
                if not text.strip():
                    continue
                try:
                    vector = embed(text)
                    if vector:
                        update_project(
                            project_name = tag,
                            item_id      = item.get("item_id", ""),
                            vector       = vector,
                            category     = item.get("category", "fyi"),
                            hierarchy    = item.get("hierarchy", "general"),
                            source       = item.get("source", ""),
                            priority     = item.get("priority", "medium"),
                        )
                except Exception as e:
                    print(f"[seed] embed {item.get('item_id')}: {e}")
            print("[seed] embedding sweep complete")
        except Exception as e:
            print(f"[seed] embedding sweep failed: {e}")

        print("[seed] starting situation formation sweep...")
        try:
            with db_lock:
                all_items = analyses.all()
            item_ids = [item.get("item_id") for item in all_items if item.get("item_id")]
            for iid in item_ids:
                try:
                    _maybe_form_situation(iid)
                except Exception as e:
                    print(f"[seed] situation sweep {iid}: {e}")
            print(f"[seed] situation sweep complete ({len(item_ids)} items)")
        except Exception as e:
            print(f"[seed] situation sweep failed: {e}")

    background_tasks.add_task(_seed_embed_and_correlate)

    # Transition state machine → reanalyzing
    _seed_job.update({
        "state":    "reanalyzing",
        "status":   "running",
        "progress": "Re-analyzing all items with new project config…",
    })
    threading.Thread(target=_run_reanalyze, daemon=True).start()

    def _monitor_reanalyze() -> None:
        import time
        time.sleep(1)
        while scan_state.get("running"):
            _seed_job["progress"] = scan_state.get("message", "Re-analyzing…")
            time.sleep(2)
        _seed_job.update({
            "state":    "scan_prompt",
            "status":   "running",
            "progress": "Re-analysis complete.",
        })

    threading.Thread(target=_monitor_reanalyze, daemon=True).start()

    return {
        "ok":             True,
        "projects_added": projects_added,
        "topics_added":   topics_added,
        "items_retagged": items_retagged,
    }


@app.post("/seed/scan")
def seed_run_scan():
    """Transition from scan_prompt → scanning, then → done."""
    global _seed_job
    if scan_state["running"]:
        raise HTTPException(status_code=409, detail="A scan is already running.")
    _seed_job.update({"state": "scanning", "status": "running", "progress": "Starting scan…"})
    sources = ["slack", "github", "jira", "outlook", "teams"]
    threading.Thread(target=_run_scan, args=(sources,), daemon=True).start()

    def _monitor_scan() -> None:
        import time
        time.sleep(1)
        while scan_state.get("running"):
            _seed_job["progress"] = scan_state.get("message", "Scanning…")
            time.sleep(2)
        _seed_job.update({"state": "done", "status": "done", "progress": "Setup complete."})

    threading.Thread(target=_monitor_scan, daemon=True).start()
    return {"ok": True}


@app.post("/seed/skip_scan")
def seed_skip_scan():
    """Transition from scan_prompt → done without running a scan."""
    global _seed_job
    _seed_job.update({"state": "done", "status": "done", "progress": "Setup complete."})
    return {"ok": True}
