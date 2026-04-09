"""
app.py — Parsival FastAPI application.

Exposes the REST API consumed by the frontend and the host sidecar scripts.
Key responsibilities:

- Receiving and deduplicating raw items via ``POST /ingest`` (sidecar path).
- Orchestrating multi-source scans via ``POST /scan`` (frontend path).
- Persisting ``Analysis`` and ``Todo`` records to SQLite via ``db.py``,
  including the context-aware enrichment fields.
- Serving settings, stats, and Slack/Teams OAuth endpoints to the frontend.
- Project and noise learning:
    - ``POST /analyses/{item_id}/tag`` — tags an item to a project and
      triggers background LLM keyword extraction and sender/group address
      extraction, merging results into the project's ``learned_keywords``
      and ``learned_senders`` lists in settings.
    - ``POST /analyses/{item_id}/noise`` — marks an item as irrelevant and
      triggers background keyword extraction into ``config.NOISE_KEYWORDS``.
- ``POST /reset`` — truncates the analyses, todos, scan_logs, embeddings, and
  situations tables while preserving saved settings.
- ``GET /projects`` — returns configured projects with learned keyword counts,
  sender counts, and embedding statistics.
- ``GET /settings`` returns ``user_name``, ``user_email``, ``focus_topics``,
  ``projects``, and ``noise_keywords`` in addition to all credential fields.
- Situation layer: ``_maybe_form_situation`` correlates related items into
  cross-source ``Situation`` records via ``correlator``.  Scores decay every
  30 minutes in a background thread (``_score_decay_loop``).
- Seed workflow: a multi-stage state machine (``POST /seed``, ``POST /seed/apply``,
  ``POST /seed/scan``) bootstraps project config from an existing corpus using
  a map-reduce LLM pass.

All AI analysis is performed asynchronously via ``agent.analyze`` so that
HTTP responses are returned immediately and the UI polls for results.

Module-level singletons:
    ``scan_state``     — Shared dict updated in-place by all background jobs for
                         progress reporting via ``GET /scan/status``.
    ``_seed_job``      — Single-slot state dict for the seed background job.
"""
import json
import logging
import secrets
import time
import threading

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

_req_log = logging.getLogger("parsival.requests")
_log = logging.getLogger("parsival")
import psutil as _psutil
_psutil.cpu_percent()  # prime interval counter so first real call is accurate
from datetime import datetime, timezone
from typing import Optional

import requests as http_requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
import crypto
import db
from agent import extract_keywords, extract_emails, resolve_owner_email, generate_project_briefing
from models import RawItem, Analysis
import correlator as _correlator
import situation_manager
import orchestrator
import seeder
import attention as _attn

app = FastAPI(title="Parsival API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    """Log every HTTP request with method, path, status, duration, and user."""
    start = time.monotonic()
    response = await call_next(request)
    ms = int((time.monotonic() - start) * 1000)
    user = request.headers.get("CF-Access-Authenticated-User-Email", "anonymous")
    status = response.status_code
    msg = "%s %s %d %dms [%s]", request.method, request.url.path, status, ms, user
    if status >= 500:
        _req_log.error(*msg)
    elif status >= 400:
        _req_log.warning(*msg)
    else:
        _req_log.info(*msg)
    return response


# Initialise the SQLite connection and schema on startup
db.conn()

# Startup diagnostics: validate database integrity
try:
    _integrity = db.conn().execute("PRAGMA integrity_check").fetchone()
    if _integrity and _integrity[0] != "ok":
        _log.error("database integrity check failed: %s", _integrity[0])
    else:
        _log.info("database integrity check passed")
except Exception as _exc:
    _log.error("database integrity check error: %s", _exc)

# Startup diagnostics: check Ollama reachability
try:
    import requests as _req
    _r = _req.get(f"{config.OLLAMA_URL.rstrip('/generate')}/api/tags", timeout=5)
    _log.info("Ollama reachable (%d models)", len(_r.json().get("models", [])))
except Exception as _exc:
    _log.warning("Ollama unreachable at startup: %s", _exc)

# Hot-load any previously saved settings on startup
_saved_settings = db.get_settings()
if _saved_settings:
    config.apply_overrides(_saved_settings)

# Resume polling for any batch jobs that were in-flight before a restart
_pending_on_startup = db.get_items_with_pending_batch()
if _pending_on_startup:
    _log.info("%d item(s) have pending batch jobs — resuming poll thread", len(_pending_on_startup))
    orchestrator._ensure_batch_poll_thread()

# Arm auto-scan timers from saved settings
_startup_schedule = (_saved_settings or {}).get("scan_schedule", {})
if _startup_schedule:
    orchestrator.scheduler_update(_startup_schedule)


# ── Compatibility shims (TinyDB-like API over SQLite for tests) ────────────────
# These proxy objects expose a minimal TinyDB-compatible surface so existing
# tests can use analyses.insert(), todos.get(doc_id=...), etc. without changes.

class _QPredicate:
    def __init__(self, field: str, val):
        self.field = field
        self.val   = val

    def __and__(self, other):
        return _QAndPredicate(self, other)


class _QAndPredicate:
    def __init__(self, left: _QPredicate, right: _QPredicate):
        self.left  = left
        self.right = right


class _QField:
    def __init__(self, field: str):
        self.field = field

    def __eq__(self, val):
        return _QPredicate(self.field, val)


class _Q:
    def __getattr__(self, field: str) -> _QField:
        return _QField(field)


Q = _Q()


def _extract_pred(pred) -> tuple:
    """Return (field, val) from a _QPredicate or TinyDB QueryInstance, or (None, None)."""
    if isinstance(pred, _QPredicate):
        return pred.field, pred.val
    # Support real TinyDB Query objects used in some test files:
    #   Q.field == val  →  QueryInstance with _hash = ('==', ('field',), val)
    h = getattr(pred, "_hash", None)
    if h and len(h) == 3 and h[0] == "==":
        field_path, val = h[1], h[2]
        if isinstance(field_path, (tuple, list)) and len(field_path) == 1:
            return field_path[0], val
    return None, None


_BOOL_COLS = frozenset({"done", "has_action", "is_passdown", "is_replied", "dismissed"})


def _coerce_bools(row: dict | None) -> dict | None:
    """Convert integer 0/1 SQLite values to Python bools for boolean columns."""
    if row is None:
        return None
    for col in _BOOL_COLS:
        if col in row and isinstance(row[col], int):
            row[col] = bool(row[col])
    return row


class _AnalysesProxy:
    """TinyDB-compatible proxy for the items table."""

    def insert(self, data: dict):
        with db.lock:
            db.upsert_item(data)

    def get(self, pred=None, doc_id=None):
        if doc_id is not None:
            return db.get_item(str(doc_id))
        field, val = _extract_pred(pred)
        if field == "item_id":
            return db.get_item(val)
        return None

    def all(self):
        return db.get_all_items()

    def upsert(self, data: dict, pred=None):
        db.upsert_item(data)

    def update(self, updates: dict, pred=None, doc_ids=None):
        if doc_ids:
            for did in doc_ids:
                db.update_item(str(did), updates)
            return
        field, val = _extract_pred(pred)
        if field == "item_id":
            db.update_item(val, updates)
        elif field == "project_tag":
            db.update_items_by_project(val, updates)

    def remove(self, pred=None, doc_ids=None):
        if doc_ids:
            for did in doc_ids:
                db.conn().execute("DELETE FROM items WHERE item_id = ?", (str(did),))
            return
        field, val = _extract_pred(pred)
        if field == "item_id":
            db.conn().execute("DELETE FROM items WHERE item_id = ?", (val,))

    def truncate(self):
        db.conn().execute("DELETE FROM items")


class _TodosProxy:
    """TinyDB-compatible proxy for the todos table."""

    def insert(self, data: dict) -> int:
        """Insert a todo and return its integer id (mirrors TinyDB doc_id)."""
        # Normalise bool → int for SQLite
        d = dict(data)
        if "done" in d and isinstance(d["done"], bool):
            d["done"] = 1 if d["done"] else 0
        with db.lock:
            return db.insert_todo(d)

    def get(self, pred=None, doc_id=None):
        if doc_id is not None:
            row = db.get_todo_by_id(doc_id)
            if row:
                row["doc_id"] = row["id"]
            return _coerce_bools(row)
        field, val = _extract_pred(pred)
        if field == "item_id":
            rows = db.get_todos_for_item(val)
            if rows:
                rows[0]["doc_id"] = rows[0]["id"]
                return _coerce_bools(rows[0])
            return None
        return None

    def all(self):
        rows = db.get_all_todos()
        for r in rows:
            r["doc_id"] = r["id"]
        return rows

    def update(self, updates: dict, pred=None, doc_ids=None):
        u = dict(updates)
        if "done" in u and isinstance(u["done"], bool):
            u["done"] = 1 if u["done"] else 0
        if doc_ids:
            for did in doc_ids:
                db.update_todo(did, u)
            return
        field, val = _extract_pred(pred)
        if field == "item_id":
            db.update_todos_for_item(val, u)

    def remove(self, pred=None, doc_ids=None):
        if doc_ids:
            for did in doc_ids:
                db.delete_todo_by_id(did)
            return
        field, val = _extract_pred(pred)
        if field == "item_id":
            db.delete_todos_for_item(val)

    def truncate(self):
        db.conn().execute("DELETE FROM todos")


class _IntelProxy:
    """TinyDB-compatible proxy for the intel table."""

    def insert(self, data: dict):
        d = dict(data)
        if "dismissed" in d and isinstance(d["dismissed"], bool):
            d["dismissed"] = 1 if d["dismissed"] else 0
        with db.lock:
            db.insert_intel(d)

    def get(self, pred=None, doc_id=None):
        if doc_id is not None:
            rows = _rows_where_id(doc_id)
            return rows
        if isinstance(pred, _QAndPredicate):
            f1, v1 = _extract_pred(pred.left)
            f2, v2 = _extract_pred(pred.right)
            if f1 == "item_id" and f2 == "fact":
                return db.get_intel_for_item(v1)[0] if db.intel_exists(v1, v2) else None
        field, val = _extract_pred(pred)
        if field == "item_id":
            rows = db.get_intel_for_item(val)
            return rows[0] if rows else None
        return None

    def all(self):
        rows = db.get_all_intel(dismissed=True)
        for r in rows:
            r["doc_id"] = r["id"]
        return rows

    def update(self, updates: dict, pred=None, doc_ids=None):
        u = dict(updates)
        if "dismissed" in u and isinstance(u["dismissed"], bool):
            u["dismissed"] = 1 if u["dismissed"] else 0
        if doc_ids:
            for did in doc_ids:
                db.update_intel_by_id(did, u)
            return
        field, val = _extract_pred(pred)
        if field == "item_id":
            if "project_tag" in u:
                db.update_intel_project(val, u["project_tag"])
            else:
                db.conn().execute(
                    "UPDATE intel SET " + ", ".join(f"{k}=?" for k in u) + " WHERE item_id = ?",
                    list(u.values()) + [val],
                )

    def remove(self, pred=None, doc_ids=None):
        if doc_ids:
            for did in doc_ids:
                db.delete_intel_by_id(did)
            return
        field, val = _extract_pred(pred)
        if field == "item_id":
            db.delete_intel_for_item(val)

    def truncate(self):
        db.conn().execute("DELETE FROM intel")


def _rows_where_id(row_id):
    row = db.conn().execute("SELECT * FROM intel WHERE id = ?", (row_id,)).fetchone()
    if row:
        d = dict(row)
        d["doc_id"] = d["id"]
        return d
    return None


class _SituationsProxy:
    """TinyDB-compatible proxy for the situations table."""

    def insert(self, data: dict):
        d = dict(data)
        if "dismissed" in d and isinstance(d["dismissed"], bool):
            d["dismissed"] = 1 if d["dismissed"] else 0
        # Keep lifecycle_status in sync with dismissed flag
        if "lifecycle_status" not in d:
            d["lifecycle_status"] = "dismissed" if d.get("dismissed") else "new"
        with db.lock:
            db.insert_situation(d)

    def get(self, pred=None):
        field, val = _extract_pred(pred)
        if field == "situation_id":
            return _coerce_bools(db.get_situation(val))
        return None

    def all(self):
        return db.get_all_situations(include_dismissed=True)

    def update(self, updates: dict, pred=None):
        field, val = _extract_pred(pred)
        if field == "situation_id":
            db.update_situation(val, updates)

    def remove(self, pred=None):
        field, val = _extract_pred(pred)
        if field == "situation_id":
            db.delete_situation(val)

    def truncate(self):
        db.conn().execute("DELETE FROM situations")


class _SettingsProxy:
    """TinyDB-compatible proxy for the settings table."""

    def get(self, pred=None, doc_id=None):
        return db.get_settings() or None

    def insert(self, data: dict):
        db.save_settings(data)

    def update(self, data: dict, pred=None, doc_ids=None):
        db.save_settings(data)

    def truncate(self):
        db.conn().execute("DELETE FROM settings")


class _ScanLogsProxy:
    """TinyDB-compatible proxy for the scan_logs table."""

    def insert(self, data: dict):
        db.insert_scan_log(data)

    def all(self):
        return db.get_all_scan_logs()

    def truncate(self):
        db.conn().execute("DELETE FROM scan_logs")


class _EmbeddingsProxy:
    """TinyDB-compatible proxy for the embeddings table (truncate only)."""

    def truncate(self):
        db.conn().execute("DELETE FROM embeddings")


class _BriefingsProxy:
    """TinyDB-compatible proxy for the briefings table (truncate only)."""

    def truncate(self):
        db.conn().execute("DELETE FROM briefings")


# Expose as module-level names so tests can import them from app
analyses       = _AnalysesProxy()
todos          = _TodosProxy()
intel_tbl      = _IntelProxy()
situations_tbl = _SituationsProxy()
settings_tbl   = _SettingsProxy()
scan_logs      = _ScanLogsProxy()
embeddings_tbl = _EmbeddingsProxy()
briefings_tbl  = _BriefingsProxy()


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_user(request: Request) -> str:
    """
    Extract the authenticated user's email from the Cloudflare Access header.

    Mirrors hexcaliper's user-scoping convention.  Falls back to
    ``"local@dev"`` for requests that bypass Cloudflare Access (e.g. local
    development without the tunnel).

    :param request: The incoming FastAPI request.
    :type request: Request
    :return: Authenticated user email, or ``"local@dev"`` if not present.
    :rtype: str
    """
    return request.headers.get("cf-access-authenticated-user-email", "local@dev")


def now_iso() -> str:
    """
    Return the current UTC time as an ISO 8601 string.

    :return: Current UTC timestamp in ISO 8601 format.
    :rtype: str
    """
    return datetime.now(timezone.utc).isoformat()


# ── Scan state ────────────────────────────────────────────────────────────────

scan_state: dict = {
    "running":                     False,
    "cancelled":                   False,
    "progress":                    0,
    "total":                       0,
    "current_source":              "",
    "current_item":                "",
    "message":                     "idle",
    "ingest_pending":              0,
    "situations_pending":          0,
    "total_items":                 0,
    "completed_items":             0,
    "estimated_minutes_remaining": 0,
}

situation_manager.init(scan_state)


def _save_analysis(a: Analysis, reanalyze: bool = False) -> None:
    """
    Upsert an ``Analysis`` into SQLite and create todo/intel rows.

    Stores all base fields plus the context-aware enrichment fields.
    Todo rows are only inserted for action items that do not already exist for
    the same ``item_id`` and ``description`` pair.  Intel rows are deduplicated
    on the same ``item_id`` and ``fact`` pair.

    Preserves ``situation_id`` from the existing record so situation membership
    is not overwritten on re-scan.  Cross-source references are re-extracted on
    every save via ``correlator.extract_references``.

    User-edited fields (``priority``, ``category``, ``project_tag``,
    ``is_passdown``) are preserved from the stored record using an ``or``
    comparison so a manually set value always wins over the LLM's fresh output,
    but a stored ``None`` falls through to the new LLM value (allowing
    previously untagged items to receive a tag on re-scan).

    When ``reanalyze=True``, existing todos and intel for this item are removed
    first so stale entries from prior analysis passes do not accumulate.

    :param a: The analysis result to persist.
    :type a: Analysis
    :param reanalyze: If ``True``, delete existing todos and intel for this
                      item before inserting fresh ones.
    :type reanalyze: bool
    """
    with db.lock:
        existing              = db.get_item(a.item_id)
        existing_situation_id = (existing or {}).get("situation_id")

        # Before wiping todos on reanalyze, snapshot any manual assignment
        # overrides (assigned_to, status) keyed by description so they survive.
        todo_overrides: dict[str, dict] = {}
        if reanalyze:
            for t in db.get_todos_for_item(a.item_id):
                if t.get("assigned_to") or t.get("status") == "assigned":
                    todo_overrides[t["description"]] = {
                        "assigned_to": t.get("assigned_to"),
                        "status":      t.get("status"),
                    }
            db.delete_todos_for_item(a.item_id)
            db.delete_intel_for_item(a.item_id)

        # Preserve fields the user has explicitly edited; let the LLM
        # reclassify everything else.  On incremental scans (reanalyze=False)
        # all existing non-null values are kept for backward compat.  On
        # reanalysis the LLM's fresh output wins EXCEPT for fields the user
        # manually changed via the UI (tracked in user_edited_fields).
        user_edited = set(
            json.loads(existing.get("user_edited_fields") or "[]")
        ) if existing else set()

        if existing and not reanalyze:
            # Incremental scan — preserve all existing non-null values
            priority    = existing.get("priority")    or a.priority
            category    = existing.get("category")    or a.category
            project_tag = existing.get("project_tag") or a.project_tag
            is_passdown = existing.get("is_passdown") or a.is_passdown
        else:
            # Reanalysis or first save — use fresh LLM values, but honour
            # any field the user has explicitly overridden.
            priority    = existing.get("priority")    if "priority"    in user_edited else a.priority
            category    = existing.get("category")    if "category"    in user_edited else a.category
            project_tag = existing.get("project_tag") if "project_tag" in user_edited else (
                              existing.get("project_tag") or a.project_tag if existing else a.project_tag
                          )
            is_passdown = existing.get("is_passdown") if "is_passdown" in user_edited else a.is_passdown

        # Extract cross-source references
        refs = _correlator.extract_references(a.title, a.body_preview or "")

        db.upsert_item({
            "item_id":            a.item_id,
            "source":             a.source,
            "direction":          a.direction,
            "title":              a.title,
            "author":             a.author,
            "timestamp":          a.timestamp,
            "url":                a.url,
            "has_action":         1 if a.has_action else 0,
            "priority":           priority,
            "category":           category,
            "task_type":          a.task_type,
            "summary":            a.summary,
            "urgency":            a.urgency_reason,
            "action_items":       json.dumps([
                {"description": x.description, "deadline": x.deadline, "owner": x.owner}
                for x in a.action_items
            ]),
            "hierarchy":          a.hierarchy,
            "is_passdown":        1 if is_passdown else 0,
            "project_tag":        project_tag,
            "conversation_id":    a.conversation_id,
            "conversation_topic": a.conversation_topic,
            "goals":              json.dumps(a.goals),
            "key_dates":          json.dumps(a.key_dates),
            "information_items":  json.dumps(a.information_items),
            "body_preview":       a.body_preview,
            "to_field":           a.to_field,
            "cc_field":           a.cc_field,
            "is_replied":         1 if a.is_replied else 0,
            "replied_at":         a.replied_at,
            "processed_at":       now_iso(),
            "situation_id":       existing_situation_id,
            "references":         json.dumps(refs),
        })

        if a.has_action and a.category != "fyi":
            for item in a.action_items:
                if not db.todo_exists(a.item_id, item.description):
                    # Auto-assign when the LLM identifies the task belongs to
                    # someone other than the user.  Resolve their email from
                    # To/CC fields; fall back to the owner name if not found.
                    auto_assigned_to = None
                    auto_status      = "open"
                    if item.owner and item.owner.lower() not in ("me", config.USER_NAME.lower()):
                        resolved = resolve_owner_email(item.owner, a.to_field, a.cc_field)
                        auto_assigned_to = resolved or item.owner
                        auto_status      = "assigned"

                    # Manual overrides (set by the user before a reanalyze) win.
                    override         = todo_overrides.get(item.description, {})
                    assigned_to      = override.get("assigned_to") or auto_assigned_to
                    status           = override.get("status")      or auto_status

                    db.insert_todo({
                        "item_id":     a.item_id,
                        "source":      a.source,
                        "title":       a.title,
                        "url":         a.url,
                        "description": item.description,
                        "deadline":    item.deadline,
                        "owner":       item.owner,
                        "priority":    a.priority,
                        "done":        0,
                        "status":      status,
                        "assigned_to": assigned_to,
                        "created_at":  now_iso(),
                    })

        for item in a.information_items:
            if not item.get("fact"):
                continue
            if not db.intel_exists(a.item_id, item["fact"]):
                db.insert_intel({
                    "item_id":     a.item_id,
                    "source":      a.source,
                    "title":       a.title,
                    "url":         a.url,
                    "fact":        item["fact"],
                    "relevance":   item.get("relevance", ""),
                    "project_tag": a.project_tag,
                    "priority":    a.priority,
                    "timestamp":   a.timestamp,
                    "dismissed":   0,
                    "created_at":  now_iso(),
                })


orchestrator.init(scan_state, save_analysis_fn=_save_analysis,
                  spawn_situation_fn=situation_manager._spawn_situation_task,
                  generate_briefing_fn=lambda: _build_briefing())

seeder.init(scan_state,
            run_scan_fn=orchestrator.run_scan,
            run_reanalyze_fn=orchestrator.run_reanalyze,
            maybe_form_situation_fn=situation_manager._maybe_form_situation)


# ── Routes ────────────────────────────────────────────────────────────────────

_MASK = "•"

def _mask(val: str) -> str:
    """
    Partially redact a credential string for safe display in the settings API.

    The first four characters are shown; the remainder is replaced with
    ``_MASK`` bullets (``•``).  The frontend uses the presence of ``•`` in a
    returned value to detect a masked placeholder and skip re-saving it.

    :param val: The raw credential string to mask.
    :type val: str
    :return: Masked string, or an empty string if ``val`` is falsy.
    :rtype: str
    """
    if not val:
        return ""
    visible = min(4, len(val))
    return val[:visible] + _MASK * max(0, len(val) - visible)


@app.get("/gpu")
def gpu_stats():
    """
    Return live GPU utilisation, VRAM usage, and temperature via NVML for all
    detected GPUs.

    Used by the frontend GPU meter widgets.  Returns ``{"ok": False}`` when
    ``pynvml`` is not installed or no NVIDIA device is present — the UI will
    fade the meters gracefully in that case.

    :return: Dict with ``ok``, and when successful: ``gpus`` — a list of
        per-device dicts each containing ``name``, ``gpu_util`` (int %),
        ``mem_used`` (bytes), ``mem_total`` (bytes), ``temperature`` (°C).
    :rtype: dict
    """
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        gpus = []
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            util   = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem    = pynvml.nvmlDeviceGetMemoryInfo(handle)
            temp   = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            name   = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            gpus.append({
                "name":        name,
                "gpu_util":    util.gpu,
                "mem_used":    mem.used,
                "mem_total":   mem.total,
                "temperature": temp,
            })
        return {"ok": True, "gpus": gpus}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/system")
def system_stats():
    """
    Return live CPU utilisation and RAM usage via psutil.

    Always available (no optional dependency).  Used by the frontend
    system meter widgets alongside the GPU meters.

    :return: Dict with ``ok``, ``cpu_util`` (int %), ``mem_used`` (bytes),
        ``mem_total`` (bytes).
    :rtype: dict
    """
    mem = _psutil.virtual_memory()
    return {
        "ok":        True,
        "cpu_util":  int(_psutil.cpu_percent(interval=None)),
        "mem_used":  mem.used,
        "mem_total": mem.total,
    }


@app.get("/health")
def health():
    """
    Service health check.

    Mirrors hexcaliper's ``/health`` response shape.  Returns ``{"ok": True}``
    plus any configuration warnings from ``config.validate()``.

    :return: Dict with ``ok`` (bool) and ``warnings`` (list of strings).
    :rtype: dict
    """
    return {"ok": True, "warnings": config.validate()}


@app.post("/reset")
def reset_db():
    """
    Truncate all data tables while preserving saved settings.

    :return: ``{"ok": True}``
    :rtype: dict
    """
    with db.lock:
        db.reset_data_tables()
    return {"ok": True}


@app.get("/senders")
def get_senders():
    """
    Return a flat sorted list of all known sender email addresses across all projects.

    Combines static ``senders`` and runtime-learned ``learned_senders`` from
    every configured project, deduplicates, and returns them sorted.  Used by
    the frontend assign-picker to offer autocomplete suggestions.

    :return: ``{"senders": [...]}`` — sorted list of unique lowercase addresses.
    :rtype: dict
    """
    seen: set[str] = set()
    for p in config.PROJECTS:
        for addr in list(p.get("senders", [])) + list(p.get("learned_senders", [])):
            if addr:
                seen.add(addr.lower())
    return {"senders": sorted(seen)}


@app.get("/projects")
def get_projects():
    """
    Return all configured projects with learning metadata.

    For each project in ``config.PROJECTS``, returns:
    - ``name``, ``keywords``, ``channels`` — static config fields.
    - ``learned_keywords``, ``learned_count`` — keywords grown at runtime via
      the tagging workflow.
    - ``learned_senders``, ``sender_count`` — email addresses grown via tagging.
    - ``embedding_items``, ``embedding_subs`` — embedding centroid stats from
      the ``embedder`` module.

    :return: List of project dicts with learning metadata.
    :rtype: list[dict]
    """
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
    """Request body for ``POST /analyses/{item_id}/tag``.

    :ivar project: Exact name of the target project (must match a configured project).
    """
    project: str


@app.patch("/analyses/{item_id}")
def patch_analysis(item_id: str, body: dict, background_tasks: BackgroundTasks):
    """
    Update editable fields on a stored analysis record.

    Accepts any subset of ``priority``, ``category``, ``project_tag``, and
    ``is_passdown``.  Only values that pass the allowed-value guard are
    applied; unknown or invalid values are silently ignored.

    Side effects:
    - Setting ``category="noise"`` also clears ``has_action`` and removes all
      associated todos.
    - Changing ``priority`` syncs the new value to all associated todo rows.
    - Changing ``project_tag`` or ``category`` triggers a background embedding
      update.

    :param item_id: Stable ID of the analysis item to update.
    :param body: Partial update dict; accepted keys: ``priority``, ``category``,
                 ``project_tag``, ``is_passdown``.
    :return: ``{"ok": True}`` plus all fields that were actually updated.
    :raises HTTPException 400: If no valid fields are present in ``body``.
    :raises HTTPException 404: If no item with ``item_id`` exists.
    """
    allowed_priorities = {"high", "medium", "low"}
    allowed_categories = {"task", "approval", "fyi", "noise"}
    allowed_task_types = {"reply", "review", None}
    updates = {}
    if "priority" in body and body["priority"] in allowed_priorities:
        updates["priority"] = body["priority"]
    if "category" in body and body["category"] in allowed_categories:
        updates["category"] = body["category"]
        if body["category"] in ("noise", "fyi"):
            updates["has_action"] = 0
    if "task_type" in body and body["task_type"] in allowed_task_types:
        updates["task_type"] = body["task_type"]
    if "project_tag" in body:
        val = body["project_tag"]
        updates["project_tag"] = db.serialize_project_tags(val) if val else None
    if "is_passdown" in body and isinstance(body["is_passdown"], bool):
        updates["is_passdown"] = 1 if body["is_passdown"] else 0
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update.")
    # Track which classification fields the user has manually edited so
    # reanalysis preserves them instead of overwriting with LLM output.
    _editable_fields = {"priority", "category", "project_tag", "is_passdown"}
    with db.lock:
        old_record = db.get_item(item_id)
        if not old_record:
            raise HTTPException(status_code=404, detail="Item not found")
        edited = set(json.loads(old_record.get("user_edited_fields") or "[]"))
        edited |= _editable_fields & updates.keys()
        updates["user_edited_fields"] = json.dumps(sorted(edited))
        db.update_item(item_id, updates)
        if updates.get("category") == "noise":
            db.delete_todos_for_item(item_id)
        if "category" in updates:
            background_tasks.add_task(_learn_keywords_for_category, old_record, updates["category"])
        elif "priority" in updates:
            db.update_todos_for_item(item_id, {"priority": updates["priority"]})
        if "project_tag" in updates:
            db.update_intel_project(item_id, updates["project_tag"])

    if "project_tag" in updates:
        situation_manager._sync_situation_tags_for_item(item_id)

    old_project  = old_record.get("project_tag")
    old_category = old_record.get("category")
    new_project  = updates.get("project_tag", old_project)
    new_category = updates.get("category", old_category)
    project_changed  = "project_tag" in updates and new_project != old_project
    category_changed = "category" in updates and new_category != old_category

    if (project_changed or category_changed) and (new_project or old_project):
        def relearn() -> None:
            """Update embeddings when a project tag or category changes on an existing item."""
            with db.lock:
                record = db.get_item(item_id)
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

    # Record attention signal for project tagging
    if "project_tag" in updates and updates["project_tag"]:
        _attn.record_action(item_id, "tagged")

    return {"ok": True, **updates}


@app.post("/analyses/{item_id}/tag")
def tag_item(item_id: str, body: TagRequest, background_tasks: BackgroundTasks):
    """
    Tag an analysis item to a project and trigger background keyword/sender learning.

    Sets the item's ``project_tag`` synchronously, then runs a background task
    (``learn``) that:

    1. Calls ``extract_keywords`` to get 5–10 characteristic keywords from the
       item's body/summary, then merges them into the project's
       ``learned_keywords`` list (capped at 100 entries).
    2. Extracts all email addresses from ``author``, ``to_field``, and
       ``cc_field``, strips the user's own address, and merges the remainder
       into the project's ``learned_senders`` list (capped at 50 entries).
    3. Persists the updated project config back to settings and calls
       ``config.apply_overrides`` so future analyses benefit immediately.
    4. Calls ``embedder.update_project`` to add/update the item's vector in
       the project's embedding centroid.

    :param item_id: Stable ID of the analysis item to tag.
    :param body: Must contain a ``project`` field matching a configured project name.
    :return: ``{"ok": True, "project": project_name}``
    :raises HTTPException 404: If the item or project does not exist.
    """
    with db.lock:
        record = db.get_item(item_id)
    if not record:
        raise HTTPException(status_code=404, detail="Item not found")

    project_name = body.project
    if not any(p.get("name") == project_name for p in config.PROJECTS):
        raise HTTPException(status_code=404, detail="Project not found")

    # Add the project to existing tags (merge, don't replace)
    with db.lock:
        existing_tags = db.parse_project_tags(record.get("project_tag"))
        if project_name not in existing_tags:
            existing_tags.append(project_name)
        new_tag_val = db.serialize_project_tags(existing_tags)

        edited = set(json.loads(record.get("user_edited_fields") or "[]"))
        edited.add("project_tag")
        db.update_item(item_id, {
            "project_tag": new_tag_val,
            "user_edited_fields": json.dumps(sorted(edited)),
        })
        db.update_intel_project(item_id, new_tag_val)
    situation_manager._sync_situation_tags_for_item(item_id)

    def learn() -> None:
        """Extract keywords and senders from the tagged item and update project config."""
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

        with db.lock:
            saved = db.get_settings()

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
        with db.lock:
            db.save_settings(saved)
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


_CATEGORY_KEYWORD_FIELD = {
    "noise":    "noise_keywords",
    "task":     "task_keywords",
    "approval": "approval_keywords",
    "fyi":      "fyi_keywords",
}


def _learn_keywords_for_category(record: dict, category: str) -> None:
    """Extract keywords from an item and merge them into the learned keyword list for its category."""
    settings_field = _CATEGORY_KEYWORD_FIELD.get(category)
    if not settings_field:
        return

    title        = record.get("title", "")
    body_preview = record.get("body_preview", "") or record.get("summary", "")
    keywords     = extract_keywords(category, title, body_preview)
    if not keywords:
        return

    with db.lock:
        saved = db.get_settings()

    existing = set(saved.get(settings_field, list(getattr(config, settings_field.upper(), []))))
    existing.update(k.lower() for k in keywords)
    saved[settings_field] = list(existing)[:200]

    with db.lock:
        db.save_settings(saved)
    config.apply_overrides(saved)
    print(f"[{category}] +{len(keywords)} keywords ({len(existing)} total)")


@app.post("/analyses/{item_id}/noise")
def mark_noise(item_id: str, background_tasks: BackgroundTasks):
    """
    Mark an analysis item as irrelevant and grow the noise keyword filter.

    Sets ``category="noise"``, ``priority="low"``, and ``has_action=False``
    synchronously, and removes all associated todos.  Then runs a background
    task (``_learn_noise_from_record``) that extracts keywords from the item
    and merges them into ``config.NOISE_KEYWORDS`` (capped at 200).

    :param item_id: Stable ID of the analysis item to mark as noise.
    :return: ``{"ok": True}``
    :raises HTTPException 404: If no item with ``item_id`` exists.
    """
    with db.lock:
        record = db.get_item(item_id)
    if not record:
        raise HTTPException(status_code=404, detail="Item not found")

    with db.lock:
        db.update_item(item_id, {"category": "noise", "priority": "low", "has_action": 0})
        db.delete_todos_for_item(item_id)

    background_tasks.add_task(_learn_keywords_for_category, record, "noise")
    _attn.record_action(item_id, "noised")

    # Build filter suggestions based on the item's fields
    suggestions = []
    if record.get("author"):
        suggestions.append({"type": "sender_contains", "value": record["author"]})
    if record.get("title"):
        suggestions.append({"type": "subject_contains", "value": record["title"][:60]})
    source = record.get("source", "")
    if source == "github":
        repo = (record.get("metadata") or {}).get("repo") or ""
        if isinstance(record.get("metadata"), str):
            import json as _json
            try:
                repo = _json.loads(record["metadata"]).get("repo", "")
            except Exception:
                repo = ""
        if repo:
            suggestions.append({"type": "source_repo", "value": repo})

    return {"ok": True, "filter_suggestions": suggestions}


@app.post("/analyses/{item_id}/action")
def record_item_action(item_id: str, body: dict):
    """
    Record a user interaction for attention model training.

    :param body: ``{"action_type": "opened"}``  (or tagged, noised, etc.)
    :return: ``{"ok": True}``
    """
    action_type = body.get("action_type", "")
    if not action_type:
        raise HTTPException(status_code=422, detail="action_type required")
    _attn.record_action(item_id, action_type)
    return {"ok": True}


@app.get("/attention/summary")
def attention_summary():
    """
    Return the attention model summary for the merLLM 'My Day' panel.

    Includes cold-start flag, centroid counts, active situation counts,
    and overdue follow-up count.
    """
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()

    active_situations = db.get_active_situations()
    overdue_count = sum(
        1 for s in active_situations
        if s.get("follow_up_date") and s["follow_up_date"] < today
    )
    new_investigating = sum(
        1 for s in active_situations
        if s.get("lifecycle_status") in ("new", "investigating")
    )

    summary = _attn.get_summary()
    summary["active_situations"]    = len(active_situations)
    summary["new_investigating"]    = new_investigating
    summary["overdue_followups"]    = overdue_count
    return summary


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/settings")
def get_settings():
    """
    Return all current configuration values for the settings UI.

    Credential fields are partially masked via ``_mask`` so the frontend can
    distinguish "set" from "not set" without exposing full secrets.

    :return: Dict of all current config values, with sensitive fields masked.
    :rtype: dict
    """
    return {
        "ollama_url":           config.OLLAMA_URL,
        "ollama_model":         config.OLLAMA_MODEL,
        "escalation_provider":  config.ESCALATION_PROVIDER,
        "escalation_model":     config.ESCALATION_MODEL,
        "escalation_api_key":   _mask(config.ESCALATION_API_KEY),
        "escalation_api_url":   config.ESCALATION_API_URL,
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
        "scan_schedule":        (db.get_settings() or {}).get("scan_schedule", {}),
        "noise_filters":        (db.get_settings() or {}).get("noise_filters", []),
        "warnings":             config.validate(),
    }


@app.post("/settings")
def save_settings(body: dict):
    """
    Persist settings to SQLite and hot-reload config.

    Merges ``body`` into the existing settings record.  Any field whose value
    is a string containing ``•`` (the mask character) is skipped — this
    prevents the frontend from accidentally overwriting a real credential with
    a masked placeholder.

    When the ``projects`` list changes, analyses tagged to removed projects
    have their ``project_tag`` cleared so no orphan tags remain in the DB.

    :param body: Partial or full settings dict.  Unknown keys are stored as-is.
    :return: ``{"ok": True, "warnings": [...]}``
    :rtype: dict
    """
    with db.lock:
        existing = db.get_settings()

    old_project_names = {p.get("name") for p in existing.get("projects", [])}

    for k, v in body.items():
        if v is not None and _MASK not in str(v):
            existing[k] = v

    new_project_names = {p.get("name") for p in existing.get("projects", [])}
    removed_projects  = old_project_names - new_project_names

    with db.lock:
        db.save_settings(existing)
        if removed_projects:
            for name in removed_projects:
                db.update_items_by_project(name, {"project_tag": None})
                # Clear from intel rows too
                for row in db.conn().execute(
                    "SELECT id, project_tag FROM intel WHERE project_tag = ? OR project_tag LIKE ?",
                    (name, f'%"{name}"%'),
                ).fetchall():
                    tags = db.parse_project_tags(row["project_tag"])
                    tags = [t for t in tags if t != name]
                    db.conn().execute(
                        "UPDATE intel SET project_tag = ? WHERE id = ?",
                        (tags[0] if tags else None, row["id"]),
                    )
            situation_manager._sync_situation_tags_all()

    config.apply_overrides(existing)
    if "scan_schedule" in body:
        orchestrator.scheduler_update(body["scan_schedule"])
    return {"ok": True, "warnings": config.validate()}


# ── Noise filters ─────────────────────────────────────────────────────────────

import noise_filter as _nf_mod


@app.get("/noise-filters")
def get_noise_filters():
    """Return the current list of noise filter rules."""
    with db.lock:
        settings = db.get_settings() or {}
    return settings.get("noise_filters", [])


@app.post("/noise-filters")
def add_noise_filter(body: dict):
    """
    Append a noise filter rule.

    :param body: ``{"type": "sender_contains", "value": "noreply@"}``
    :return: Updated filter list.
    :raises HTTPException 422: If the rule is invalid.
    """
    err = _nf_mod.validate_rule(body)
    if err:
        raise HTTPException(status_code=422, detail=err)
    with db.lock:
        settings = db.get_settings() or {}
        rules: list = settings.get("noise_filters", [])
        rules.append({"type": body["type"], "value": body["value"].strip()})
        settings["noise_filters"] = rules
        db.save_settings(settings)
    return rules


@app.delete("/noise-filters/{index}")
def delete_noise_filter(index: int):
    """
    Remove a noise filter rule by its zero-based index.

    :param index: Zero-based index of the rule to remove.
    :return: Updated filter list.
    :raises HTTPException 404: If index is out of range.
    """
    with db.lock:
        settings = db.get_settings() or {}
        rules: list = settings.get("noise_filters", [])
        if index < 0 or index >= len(rules):
            raise HTTPException(status_code=404, detail="Filter index out of range.")
        rules.pop(index)
        settings["noise_filters"] = rules
        db.save_settings(settings)
    return rules


@app.get("/noise-filters/count")
def count_filtered_items():
    """Return the number of items stored with category='filtered'."""
    with db.lock:
        n = db.conn().execute(
            "SELECT COUNT(*) FROM items WHERE category='filtered'"
        ).fetchone()[0]
    return {"count": n}


# ── Ingest (POST target for host sidecar scripts) ─────────────────────────────

class IngestRequest(BaseModel):
    """Request body for ``POST /ingest``.

    :ivar items: List of raw item dicts.  Each dict must have an ``item_id``
                 key; all other fields correspond to ``RawItem`` fields.
    """
    items: list[dict]


@app.post("/ingest")
def ingest(body: IngestRequest, background_tasks: BackgroundTasks):
    """
    Receive raw items from host sidecar scripts (Outlook, Thunderbird, etc.).

    Deduplicates by ``item_id`` against the items table — items that have
    already been processed are silently skipped.  New items are queued as a
    background task so the HTTP response is returned immediately.

    :param body: List of raw item dicts.
    :return: ``{"received": N, "skipped": M}``
    :rtype: dict
    """
    raw: list[RawItem] = []
    for i in body.items:
        iid = i.get("item_id", "")
        if not iid:
            continue
        with db.lock:
            if db.get_item(iid):
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

    if raw:
        background_tasks.add_task(orchestrator.process_ingest_items, raw)

    return {"received": len(raw), "skipped": len(body.items) - len(raw)}


# ── Scan ──────────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    """Request body for ``POST /scan``.

    :ivar sources: Connector names to fetch from.  Defaults to all four
                   standard connectors.
    """
    sources: list[str] = ["slack", "github", "jira", "outlook"]


@app.post("/scan")
def start_scan(body: ScanRequest):
    """
    Start a multi-source scan in the background.

    Returns immediately; poll ``GET /scan/status`` for progress.

    :param body: Scan request specifying which sources to include.
    :return: ``{"status": "started", "sources": [...]}``
    :raises HTTPException 409: If a scan or re-analysis is already running.
    """
    if scan_state["running"]:
        raise HTTPException(status_code=409, detail="Scan already in progress.")
    threading.Thread(target=orchestrator.run_scan, args=(body.sources,), daemon=True).start()
    return {"status": "started", "sources": body.sources}


@app.get("/scan/status")
def scan_status():
    """
    Return the current scan/ingest/reanalyze progress state.

    :return: Current ``scan_state`` dict plus ``auto_scans`` schedule status.
    :rtype: dict
    """
    return {**scan_state, "auto_scans": orchestrator.get_schedule_status()}


@app.post("/scan/cancel")
def cancel_scan():
    """
    Signal a running scan to stop after the current item finishes.

    :return: ``{"ok": True}`` if a scan was running, else ``{"ok": False, ...}``.
    :rtype: dict
    """
    if not scan_state["running"]:
        return {"ok": False, "detail": "No scan running"}
    scan_state["cancelled"] = True
    return {"ok": True}


@app.post("/analysis/stop")
def stop_all_analysis():
    """
    Gracefully halt all ongoing analysis activity.

    :return: ``{"ok": True}``
    :rtype: dict
    """
    scan_state["cancelled"] = True
    seeder.cancel()
    return {"ok": True}


@app.post("/reanalyze")
def start_reanalyze():
    """
    Re-run LLM analysis on all stored items using the current config.

    Returns immediately; poll ``GET /scan/status`` for progress.

    :return: ``{"status": "started", "item_count": N}``
    :raises HTTPException 409: If a scan or re-analysis is already running.
    """
    if scan_state["running"]:
        raise HTTPException(status_code=409, detail="A scan or re-analysis is already running.")
    with db.lock:
        count = db.count_items()
    threading.Thread(target=orchestrator.run_reanalyze, daemon=True).start()
    return {"status": "started", "item_count": count}


@app.get("/reanalyze/count")
def reanalyze_count():
    """
    Return the number of stored items that would be processed by ``POST /reanalyze``.

    :return: ``{"count": N}``
    :rtype: dict
    """
    with db.lock:
        return {"count": db.count_items()}


# ── Todos ─────────────────────────────────────────────────────────────────────

@app.get("/todos")
def get_todos(
    source:   Optional[str] = None,
    priority: Optional[str] = None,
    done:     bool          = False,
):
    """
    Return action-item todos, optionally filtered and sorted by priority.

    By default only open (``done=False``) items are returned.  Results are
    sorted by priority (high → medium → low) then by creation time ascending.
    A ``doc_id`` field is added to every returned row for use in PATCH/DELETE.

    :param source: Filter to items from a specific connector.
    :param priority: Filter to items with a specific priority level.
    :param done: If ``True``, include completed items.
    :return: List of todo dicts sorted by priority then creation time.
    :rtype: list[dict]
    """
    results = db.get_todos(
        done=done,
        source=source,
        priority=priority,
    )
    for t in results:
        t["doc_id"] = t["id"]
        if "status" not in t:
            t["status"] = "done" if t.get("done") else "open"
    return results


@app.post("/todos")
def create_todo(body: dict):
    """
    Create a manual action item.

    Manual todos are not tied to LLM analysis — they represent work the user
    wants to track themselves.  The ``item_id`` field is optional; when
    supplied the todo is associated with an existing analysis item.

    :param body: Dict with required ``description`` and optional ``deadline``,
                 ``priority``, ``project_tag``, ``item_id``.
    :return: ``{"ok": True, "doc_id": <id>}``
    :raises HTTPException 400: If ``description`` is missing or empty.
    """
    description = (body.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    allowed_priorities = {"high", "medium", "low"}
    priority = body.get("priority", "medium")
    if priority not in allowed_priorities:
        priority = "medium"
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "description": description,
        "priority":    priority,
        "is_manual":   1,
        "done":        0,
        "status":      "open",
        "created_at":  now,
        "source":      "manual",
        "title":       "",
        "url":         "",
        "owner":       "me",
    }
    if body.get("deadline"):
        data["deadline"] = body["deadline"]
    if body.get("project_tag"):
        data["project_tag"] = body["project_tag"]
    if body.get("item_id"):
        data["item_id"] = body["item_id"]
        with db.lock:
            item = db.get_item(body["item_id"])
        if item:
            data["source"]      = item.get("source", "manual")
            data["title"]       = item.get("title", "")
            data["url"]         = item.get("url", "")
            data["project_tag"] = data.get("project_tag") or item.get("project_tag")
    with db.lock:
        doc_id = db.insert_todo(data)
    return {"ok": True, "doc_id": doc_id}


@app.patch("/todos/{doc_id}")
def patch_todo(doc_id: int, body: dict):
    """
    Update a todo item.

    Accepted fields: ``status``, ``done``, ``assigned_to``, ``description``,
    ``deadline``, ``priority``, ``project_tag``.

    :param doc_id: Integer id of the todo record.
    :param body: Partial update dict.
    :return: ``{"ok": True}``
    :rtype: dict
    """
    updates = {}
    if "status" in body and body["status"] in ("open", "done", "assigned"):
        updates["status"] = body["status"]
        updates["done"]   = 1 if body["status"] == "done" else 0
    elif "done" in body:
        done = bool(body["done"])
        updates["done"]   = 1 if done else 0
        updates["status"] = "done" if done else "open"
    if "assigned_to" in body:
        updates["assigned_to"] = body["assigned_to"] or None
    if "description" in body:
        desc = (body["description"] or "").strip()
        if desc:
            updates["description"] = desc
    if "deadline" in body:
        updates["deadline"] = body["deadline"] or None
    if "priority" in body and body["priority"] in ("high", "medium", "low"):
        updates["priority"] = body["priority"]
    if "project_tag" in body:
        updates["project_tag"] = body["project_tag"] or None
    if updates:
        with db.lock:
            db.update_todo(doc_id, updates)
    return {"ok": True}


@app.delete("/todos/{doc_id}")
def delete_todo(doc_id: int):
    """
    Permanently delete a todo item by its integer id.

    :param doc_id: Integer id of the todo record to remove.
    :return: HTTP 204 No Content.
    """
    with db.lock:
        db.delete_todo_by_id(doc_id)
    return Response(status_code=204)


# ── Intel ──────────────────────────────────────────────────────────────────────

@app.get("/intel")
def get_intel(
    source:             Optional[str] = None,
    project:            Optional[str] = None,
    include_dismissed:  bool          = False,
):
    """
    Return intel (information) items sorted by timestamp descending.

    A ``doc_id`` field is added to each returned row.

    :param source: Filter to items from a specific connector.
    :param project: Filter to items tagged to a specific project.
    :param include_dismissed: When ``True``, dismissed items are included.
    :return: List of intel dicts sorted newest-first.
    :rtype: list[dict]
    """
    results = db.get_all_intel(dismissed=include_dismissed)
    if source:
        results = [r for r in results if r.get("source") == source]
    if project:
        results = [r for r in results if project in db.parse_project_tags(r.get("project_tag"))]
    results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    for r in results:
        r["doc_id"] = r["id"]
    return results


@app.delete("/intel/{doc_id}")
def delete_intel(doc_id: int):
    """
    Permanently delete an intel item by its integer id.

    :param doc_id: Integer id of the intel record to remove.
    :return: HTTP 204 No Content.
    """
    with db.lock:
        db.delete_intel_by_id(doc_id)
    return Response(status_code=204)


@app.patch("/intel/{doc_id}")
def patch_intel(doc_id: int, body: dict):
    """
    Update an intel item, currently limited to toggling the ``dismissed`` flag.

    :param doc_id: Integer id of the intel record.
    :param body: Partial update dict; accepted key: ``dismissed`` (bool).
    :return: ``{"ok": True}``
    :rtype: dict
    """
    if "dismissed" in body:
        with db.lock:
            db.update_intel_by_id(doc_id, {"dismissed": 1 if body["dismissed"] else 0})
    return {"ok": True}


# ── Briefing ──────────────────────────────────────────────────────────────────

def _build_briefing() -> dict:
    """
    Generate a project-status briefing using the LLM.

    Only projects (and the untagged pool) that have had intel, situation, or
    todo activity since the last briefing are included, saving LLM tokens.

    :return: Briefing dict with ``generated_at`` and ``sections`` list.
    :rtype: dict
    """
    with db.lock:
        last        = db.get_briefing()
        all_intel   = db.get_all_intel(dismissed=False)
        all_todos   = db.get_todos(done=False)
        all_sits    = db.get_all_situations(include_dismissed=False)
        all_items   = db.get_all_items()

    cutoff = last["generated_at"] if last else "1970-01-01T00:00:00+00:00"

    # Collect project tags with activity since last briefing.
    active_projects: set[str] = set()
    has_untagged = False
    for i in all_intel:
        if (i.get("created_at") or "") > cutoff:
            tags = db.parse_project_tags(i.get("project_tag"))
            if tags:
                active_projects.update(tags)
            else:
                has_untagged = True
    for s in all_sits:
        if (s.get("last_updated") or "") > cutoff:
            tags = db.parse_project_tags(s.get("project_tag"))
            if tags:
                active_projects.update(tags)
            else:
                has_untagged = True
    for t in all_todos:
        if (t.get("created_at") or "") > cutoff:
            item = next((a for a in all_items if a.get("item_id") == t.get("item_id")), {})
            tags = db.parse_project_tags(item.get("project_tag"))
            if tags:
                active_projects.update(tags)
            else:
                has_untagged = True

    sections = []
    for project in sorted(active_projects):
        intel_facts  = [i["fact"] for i in all_intel    if project in db.parse_project_tags(i.get("project_tag"))]
        sit_lines    = [f"{s['title']} ({s.get('status','')}"
                        f"{' — score '+str(round(s['score'],1)) if s.get('score') else ''})"
                        for s in all_sits if project in db.parse_project_tags(s.get("project_tag"))]
        item_ids     = {a["item_id"] for a in all_items if db.item_has_project(a, project)}
        todo_descs   = [t["description"] for t in all_todos if t.get("item_id") in item_ids]
        sit_refs     = [{"situation_id": s["situation_id"], "title": s["title"]}
                        for s in all_sits if project in db.parse_project_tags(s.get("project_tag"))]
        todo_refs    = [{"doc_id": t["id"], "description": t["description"],
                         "priority": t.get("priority","medium")}
                        for t in all_todos if t.get("item_id") in item_ids]

        summary = generate_project_briefing(project, intel_facts, sit_lines, todo_descs)
        sections.append({
            "project":    project,
            "summary":    summary,
            "situations": sit_refs,
            "todos":      todo_refs,
        })

    # Untagged pool — only if active.
    if has_untagged:
        untagged_items = {a["item_id"] for a in all_items if not db.item_has_any_project(a)}
        intel_facts  = [i["fact"] for i in all_intel  if not db.parse_project_tags(i.get("project_tag"))]
        sit_lines    = [f"{s['title']} ({s.get('status','')})"
                        for s in all_sits if not db.parse_project_tags(s.get("project_tag"))]
        todo_descs   = [t["description"] for t in all_todos if t.get("item_id") in untagged_items]
        sit_refs     = [{"situation_id": s["situation_id"], "title": s["title"]}
                        for s in all_sits if not db.parse_project_tags(s.get("project_tag"))]
        todo_refs    = [{"doc_id": t["id"], "description": t["description"],
                         "priority": t.get("priority","medium")}
                        for t in all_todos if t.get("item_id") in untagged_items]
        summary = generate_project_briefing("General", intel_facts, sit_lines, todo_descs)
        sections.append({
            "project":    None,
            "summary":    summary,
            "situations": sit_refs,
            "todos":      todo_refs,
        })

    return {"sections": sections}


@app.get("/briefing")
def get_briefing():
    """
    Return the latest cached briefing, or an empty response if none exists.

    :return: Briefing dict with ``generated_at`` and ``sections``, or ``{}``.
    :rtype: dict
    """
    with db.lock:
        briefing = db.get_briefing()
    return briefing or {}


@app.post("/briefing/generate")
def generate_briefing(background_tasks: BackgroundTasks):
    """
    Trigger briefing generation in the background.

    :return: ``{"ok": True}``
    :rtype: dict
    """
    def _run():
        content = _build_briefing()
        with db.lock:
            db.save_briefing(content)
        print(f"[briefing] generated {len(content.get('sections', []))} sections")

    background_tasks.add_task(_run)
    return {"ok": True}


# ── Analyses ──────────────────────────────────────────────────────────────────

def _deserialize_analysis(a: dict) -> dict:
    """
    Deserialize JSON-string fields and normalise legacy field names for the frontend.

    Also renames the legacy ``"urgency"`` key to ``"urgency_reason"`` for any
    records written before that field was renamed.

    :param a: Raw analysis record dict as returned by db.py.
    :type a: dict
    :return: The same dict with JSON fields parsed and field names normalised.
    :rtype: dict
    """
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
    """
    Return stored analysis records with optional filtering.

    All filters are applied sequentially (AND logic).  Results are sorted by
    ``timestamp`` descending.  JSON-encoded fields are deserialized via
    ``_deserialize_analysis`` before returning.

    :param source: Filter to a specific connector.
    :param category: Filter by category.
    :param hierarchy: Filter by hierarchy tier.
    :param project: Filter by project tag.  Pass ``"__none__"`` to return only
                    untagged items.
    :param q: Full-text search across ``title``, ``summary``, ``author``, and
              ``body_preview`` (case-insensitive substring match).
    :param from_date: ISO 8601 lower bound on ``timestamp`` (inclusive).
    :param to_date: ISO 8601 upper bound on ``timestamp`` (inclusive).
    :param limit: Maximum number of results to return. Defaults to 1000.
    :return: List of deserialized analysis dicts sorted newest-first.
    :rtype: list[dict]
    """
    with db.lock:
        results = db.get_all_items()

    if source:
        results = [a for a in results if a.get("source") == source]
    if category:
        results = [a for a in results if a.get("category") == category]
    if hierarchy:
        results = [a for a in results if a.get("hierarchy") == hierarchy]
    if project == "__none__":
        results = [a for a in results if not db.item_has_any_project(a)]
    elif project:
        results = [a for a in results if db.item_has_project(a, project)]
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
    sliced = results[:limit]

    # Attach attention scores (fast path — reads stored vectors; 0.5 on cold start)
    cold = _attn.is_cold_start()
    out  = []
    for a in sliced:
        rec = _deserialize_analysis(dict(a))
        if cold:
            rec["attention_score"] = 0.5
        else:
            try:
                from embedder import get_item_vector
                vec   = get_item_vector(a.get("item_id", ""))
                score = _attn.compute_score(vec or [])
            except Exception:
                score = 0.5
            rec["attention_score"] = round(score, 4)
        out.append(rec)
    return out


_ACTIVE_LIFECYCLE = {"new", "investigating", "waiting"}
_ALL_LIFECYCLE    = {"new", "investigating", "waiting", "resolved", "dismissed"}


@app.get("/situations")
def get_situations(
    project:             Optional[str] = None,
    status:              Optional[str] = None,
    lifecycle_status:    Optional[str] = None,
    min_score:           float         = 0.0,
    include_dismissed:   bool          = False,
    include_resolved:    bool          = False,
):
    """
    Return situations, filtered and sorted by score descending.

    Default view: ``new``, ``investigating``, and ``waiting`` situations.
    Pass ``include_resolved=true`` to also show ``resolved``.
    Pass ``include_dismissed=true`` to also show ``dismissed``.
    Pass ``lifecycle_status=<value>`` to filter to an exact lifecycle status.
    """
    with db.lock:
        all_sits = db.get_all_situations(include_dismissed=True)

    # Lifecycle filter
    if lifecycle_status:
        all_sits = [s for s in all_sits if s.get("lifecycle_status") == lifecycle_status]
    else:
        allowed = set(_ACTIVE_LIFECYCLE)
        if include_resolved:
            allowed.add("resolved")
        if include_dismissed:
            allowed.add("dismissed")
        all_sits = [s for s in all_sits if s.get("lifecycle_status", "new") in allowed]

    if project:
        all_sits = [s for s in all_sits if project in db.parse_project_tags(s.get("project_tag"))]
    if status:
        all_sits = [s for s in all_sits if s.get("status") == status]
    if min_score:
        all_sits = [s for s in all_sits if s.get("score", 0) >= min_score]
    all_sits.sort(key=lambda s: s.get("score", 0), reverse=True)
    return [situation_manager._situation_response(s) for s in all_sits]


@app.get("/situations/{situation_id}")
def get_situation(situation_id: str):
    """
    Return a single situation with all contributing analyses fully deserialized.

    :param situation_id: UUID of the situation to retrieve.
    :return: Full situation dict with deserialized ``items`` list.
    :raises HTTPException 404: If no situation with the given ID exists.
    """
    with db.lock:
        sit = db.get_situation(situation_id)
    if not sit:
        raise HTTPException(status_code=404, detail="Situation not found")
    resp = situation_manager._situation_response(sit)
    # Replace lightweight items with fully deserialized analyses
    item_ids = sit.get("item_ids", [])
    with db.lock:
        full_items = [db.get_item(iid) for iid in item_ids]
    resp["items"] = [_deserialize_analysis(dict(r)) for r in full_items if r]
    return resp


@app.post("/situations/{situation_id}/dismiss")
def dismiss_situation(situation_id: str, body: dict = {}):
    """
    Mark a situation as dismissed.

    :param situation_id: UUID of the situation to dismiss.
    :param body: Optional dict with a ``reason`` key.
    :return: ``{"ok": True}``
    :raises HTTPException 404: If no situation with the given ID exists.
    """
    with db.lock:
        sit = db.get_situation(situation_id)
        if not sit:
            raise HTTPException(status_code=404, detail="Situation not found")
        prev = sit.get("lifecycle_status", "new")
        db.update_situation(
            situation_id,
            {"dismissed": 1, "dismiss_reason": body.get("reason"),
             "lifecycle_status": "dismissed"},
        )
        db.insert_situation_event(situation_id, prev, "dismissed", body.get("reason"))
    return {"ok": True}


@app.post("/situations/{situation_id}/undismiss")
def undismiss_situation(situation_id: str):
    """
    Restore a previously dismissed situation.

    :param situation_id: UUID of the situation to restore.
    :return: ``{"ok": True}``
    :raises HTTPException 404: If no situation with the given ID exists.
    """
    with db.lock:
        sit = db.get_situation(situation_id)
        if not sit:
            raise HTTPException(status_code=404, detail="Situation not found")
        db.update_situation(
            situation_id,
            {"dismissed": 0, "dismiss_reason": None, "lifecycle_status": "new"},
        )
        db.insert_situation_event(situation_id, "dismissed", "new", "restored")
    return {"ok": True}


@app.post("/situations/{situation_id}/rescore")
def rescore_situation(situation_id: str):
    """
    Manually trigger a full score recomputation and LLM re-synthesis for a situation.

    :param situation_id: UUID of the situation to rescore.
    :return: Updated situation response dict.
    :raises HTTPException 404: If no situation with the given ID exists.
    """
    with db.lock:
        sit = db.get_situation(situation_id)
    if not sit:
        raise HTTPException(status_code=404, detail="Situation not found")
    situation_manager._update_situation_record(situation_id, sit.get("item_ids", []))
    with db.lock:
        updated = db.get_situation(situation_id)
    return situation_manager._situation_response(updated)


@app.patch("/situations/{situation_id}")
def patch_situation(situation_id: str, body: dict):
    """
    Manually override editable fields on a situation record.

    Only ``title``, ``status``, and ``project_tag`` may be changed this way.

    :param situation_id: UUID of the situation to update.
    :param body: Partial update dict; accepted keys: ``title``, ``status``,
                 ``project_tag``.
    :return: ``{"ok": True}`` plus all fields that were applied.
    :raises HTTPException 400: If no valid fields are present in ``body``.
    :raises HTTPException 404: If no situation with the given ID exists.
    """
    allowed = {"title", "status", "project_tag", "lifecycle_status", "follow_up_date", "notes"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update.")

    if "lifecycle_status" in updates and updates["lifecycle_status"] not in _ALL_LIFECYCLE:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid lifecycle_status. Valid values: {sorted(_ALL_LIFECYCLE)}",
        )

    with db.lock:
        sit = db.get_situation(situation_id)
        if not sit:
            raise HTTPException(status_code=404, detail="Situation not found")
        if "lifecycle_status" in updates:
            prev = sit.get("lifecycle_status", "new")
            # Keep dismissed flag in sync
            if updates["lifecycle_status"] == "dismissed":
                updates["dismissed"] = 1
            elif sit.get("dismissed"):
                updates["dismissed"] = 0
            db.insert_situation_event(situation_id, prev, updates["lifecycle_status"])
        db.update_situation(situation_id, updates)
    return {"ok": True, **updates}


@app.post("/situations/{situation_id}/transition")
def transition_situation(situation_id: str, body: dict):
    """
    Transition a situation to a new lifecycle status and log the event.

    :param body: ``{"to_status": "<status>", "note": "<optional note>",
                    "follow_up_date": "<optional ISO date>"}``
    :return: ``{"ok": True, "lifecycle_status": "<new status>"}``
    :raises HTTPException 404: If no situation exists.
    :raises HTTPException 422: If ``to_status`` is invalid.
    """
    to_status = body.get("to_status")
    if not to_status or to_status not in _ALL_LIFECYCLE:
        raise HTTPException(
            status_code=422,
            detail=f"to_status required. Valid values: {sorted(_ALL_LIFECYCLE)}",
        )
    with db.lock:
        sit = db.get_situation(situation_id)
        if not sit:
            raise HTTPException(status_code=404, detail="Situation not found")
        prev    = sit.get("lifecycle_status", "new")
        updates = {"lifecycle_status": to_status}
        if to_status == "dismissed":
            updates["dismissed"] = 1
        elif sit.get("dismissed"):
            updates["dismissed"] = 0
        if "follow_up_date" in body:
            updates["follow_up_date"] = body["follow_up_date"]
        db.update_situation(situation_id, updates)
        db.insert_situation_event(situation_id, prev, to_status, body.get("note"))
        item_ids = sit.get("item_ids", [])

    # Record attention signals for all items in this situation
    action = "investigated_situation" if to_status == "investigating" else \
             "dismissed_situation"     if to_status == "dismissed"    else None
    if action and item_ids:
        for iid in item_ids:
            _attn.record_action(iid, action)

    return {"ok": True, "lifecycle_status": to_status}


@app.get("/situations/{situation_id}/events")
def get_situation_events(situation_id: str):
    """
    Return the lifecycle event history for a situation, oldest first.

    :param situation_id: UUID of the situation.
    :return: List of event dicts with ``from_status``, ``to_status``,
             ``timestamp``, and ``note``.
    :raises HTTPException 404: If no situation exists.
    """
    with db.lock:
        if not db.get_situation(situation_id):
            raise HTTPException(status_code=404, detail="Situation not found")
        events = db.get_situation_events(situation_id)
    return events


@app.post("/situations/{situation_id}/deep-analysis")
def submit_deep_analysis(situation_id: str):
    """
    Submit a situation for extended-context deep analysis via merLLM's batch API.

    Builds a prompt from the situation's title, summary, and contributing items,
    then queues it for processing during night mode (qwen3:32b, 32K+ context).

    :param situation_id: UUID of the situation to analyse.
    :return: ``{"ok": True, "job_id": "..."}``
    :raises HTTPException 404: If no situation with the given ID exists.
    :raises HTTPException 502: If merLLM is unreachable.
    """
    with db.lock:
        sit = db.get_situation(situation_id)
    if not sit:
        raise HTTPException(status_code=404, detail="Situation not found")

    item_ids = sit.get("item_ids", [])
    with db.lock:
        items = [db.get_item(iid) for iid in item_ids if db.get_item(iid)]

    items_text = "\n".join(
        f"- [{i.get('source','?')}] {i.get('title','')}: {i.get('summary','')}"
        for i in items if i
    )
    actions_text = "\n".join(
        f"- {a.get('description','')}" for a in (sit.get("open_actions") or [])
    ) or "None identified."

    prompt = (
        f"You are analysing an operational situation. Provide a deep, thorough analysis — "
        f"explore implications, root causes, risks, and recommended actions. "
        f"Do not summarize; go deeper than the existing summary.\n\n"
        f"Situation: {sit.get('title','')}\n"
        f"Summary: {sit.get('summary','')}\n"
        f"Score: {sit.get('score', 0):.2f}  Priority: {sit.get('priority','unknown')}\n\n"
        f"Contributing items ({len(items)}):\n{items_text or 'None.'}\n\n"
        f"Open actions:\n{actions_text}"
    )

    try:
        r = http_requests.post(
            f"{config.MERLLM_URL}/api/batch/submit",
            json={"source_app": "parsival", "prompt": prompt},
            timeout=10,
        )
        r.raise_for_status()
        job_id = r.json().get("id")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"merLLM unreachable: {exc}")

    # Record attention signals
    for iid in item_ids:
        _attn.record_action(iid, "deep_analysis")

    return {"ok": True, "job_id": job_id}


@app.post("/situations/{situation_id}/deep-analysis/save")
def save_deep_analysis(situation_id: str, body: dict):
    """
    Fetch a completed batch job result from merLLM and store it as an intel item
    linked to the situation.

    :param situation_id: UUID of the situation.
    :param body: Must contain ``job_id``.
    :return: ``{"ok": True}``
    :raises HTTPException 404: If situation or job not found.
    :raises HTTPException 409: If job is not yet completed.
    :raises HTTPException 502: If merLLM is unreachable.
    """
    job_id = (body.get("job_id") or "").strip()
    if not job_id:
        raise HTTPException(status_code=422, detail="job_id is required")

    with db.lock:
        sit = db.get_situation(situation_id)
    if not sit:
        raise HTTPException(status_code=404, detail="Situation not found")

    try:
        r = http_requests.get(
            f"{config.MERLLM_URL}/api/batch/results/{job_id}", timeout=10
        )
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="Job not found")
        if r.status_code == 409:
            raise HTTPException(status_code=409, detail="Job not yet completed")
        r.raise_for_status()
        result_text = r.json().get("result", "")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"merLLM unreachable: {exc}")

    item_ids = sit.get("item_ids", [])
    anchor_item_id = item_ids[0] if item_ids else None
    with db.lock:
        db.insert_intel({
            "item_id":    anchor_item_id,
            "source":     "deep_analysis",
            "fact":       result_text,
            "relevance":  f"Extended-context deep analysis of situation: {sit.get('title','')}",
            "project_tag": sit.get("project_tag"),
            "dismissed":  0,
        })

    return {"ok": True}


@app.get("/batch/status/{job_id}")
def proxy_batch_status(job_id: str):
    """
    Proxy GET /api/batch/status/{job_id} to merLLM.

    :param job_id: Batch job UUID.
    :return: Job status dict from merLLM.
    :raises HTTPException 404: If job not found.
    :raises HTTPException 502: If merLLM is unreachable.
    """
    try:
        r = http_requests.get(
            f"{config.MERLLM_URL}/api/batch/status/{job_id}", timeout=5
        )
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="Job not found")
        return r.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"merLLM unreachable: {exc}")


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/stats")
def get_stats():
    """
    Return aggregate statistics for the dashboard summary bar.

    :return: Dict with counts and breakdowns.
    :rtype: dict
    """
    with db.lock:
        all_a      = db.get_all_items()
        open_todos = db.get_todos(done=False)
        done_todos = db.get_todos(done=True)
        logs       = db.get_all_scan_logs()

    by_source: dict[str, int] = {}
    for t in open_todos:
        s = t.get("source", "unknown")
        by_source[s] = by_source.get(s, 0) + 1

    by_category: dict[str, int] = {}
    for a in all_a:
        c = a.get("category", "unknown")
        by_category[c] = by_category.get(c, 0) + 1

    with db.lock:
        all_sits   = db.get_all_situations(include_dismissed=True)
        open_intel = db.get_all_intel(dismissed=False)

    last_scan = logs[0] if logs else None

    return {
        "total_items":           len(all_a),
        "open_todos":            len(open_todos),
        "done_todos":            len([t for t in done_todos if t.get("done")]),
        "high_priority":         sum(1 for t in open_todos if t.get("priority") == "high"),
        "open_intel":            len(open_intel),
        "by_source":             [{"source": k, "count": v} for k, v in by_source.items()],
        "by_category":           [{"category": k, "count": v} for k, v in by_category.items()],
        "last_scan":             last_scan,
        "open_situations":       len([s for s in all_sits if not s.get("dismissed")]),
        "high_score_situations": len([s for s in all_sits
                                      if not s.get("dismissed") and s.get("score", 0) >= 1.5]),
    }


# Warn at import time if OAuth tokens will be stored unencrypted.
if not config.CREDENTIALS_KEY:
    import logging as _log
    _log.getLogger(__name__).warning(
        "CREDENTIALS_KEY is not set — OAuth tokens will be stored unencrypted in SQLite."
    )

# ── OAuth state nonce store ────────────────────────────────────────────────────
# Maps state token → expiry timestamp. Validated in each OAuth callback.
_oauth_states: dict[str, float] = {}
_OAUTH_STATE_TTL = 600  # 10 minutes


def _new_oauth_state() -> str:
    """Generate a cryptographically random state token and store it with a TTL."""
    _clean_oauth_states()
    token = secrets.token_urlsafe(32)
    _oauth_states[token] = time.time() + _OAUTH_STATE_TTL
    return token


def _validate_oauth_state(state: str | None) -> bool:
    """Return True if the state token is present and not expired, then remove it."""
    _clean_oauth_states()
    if not state or state not in _oauth_states:
        return False
    del _oauth_states[state]
    return True


def _clean_oauth_states() -> None:
    """Remove expired state tokens."""
    now = time.time()
    expired = [k for k, exp in _oauth_states.items() if exp < now]
    for k in expired:
        del _oauth_states[k]


# ── Slack OAuth ────────────────────────────────────────────────────────────────

_SLACK_REDIRECT_URI  = config.SLACK_REDIRECT_URI
_SLACK_USER_SCOPES   = (
    "channels:history,channels:read,groups:history,groups:read,"
    "im:history,im:read,mpim:history,mpim:read,search:read,users:read"
)


@app.get("/slack/connect")
def slack_connect():
    """
    Begin the Slack OAuth2 user-token flow.

    :return: HTTP 302 redirect to the Slack authorization page.
    :raises HTTPException 400: If ``SLACK_CLIENT_ID`` is not yet configured.
    """
    if not config.SLACK_CLIENT_ID:
        raise HTTPException(status_code=400, detail="SLACK_CLIENT_ID not configured — save it in Settings first.")
    state = _new_oauth_state()
    url = (
        f"https://slack.com/oauth/v2/authorize"
        f"?client_id={config.SLACK_CLIENT_ID}"
        f"&user_scope={_SLACK_USER_SCOPES}"
        f"&redirect_uri={_SLACK_REDIRECT_URI}"
        f"&state={state}"
    )
    return Response(status_code=302, headers={"Location": url})


@app.get("/slack/callback")
def slack_callback(code: str = None, error: str = None, state: str = None):
    """
    Handle the Slack OAuth2 redirect callback.

    :param code: Authorization code returned by Slack.
    :param error: Error identifier returned by Slack if the user denied access.
    :param state: CSRF state nonce generated in ``/slack/connect``.
    :return: HTTP 302 redirect.
    :raises HTTPException 400: If no ``code`` is provided and no ``error`` is set.
    :raises HTTPException 403: If the ``state`` parameter is missing or invalid.
    """
    if error:
        return Response(status_code=302, headers={"Location": f"/page/?slack_error={error}"})
    if not _validate_oauth_state(state):
        raise HTTPException(status_code=403, detail="Invalid or expired OAuth state — possible CSRF attempt.")
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
    print(f"[slack/callback] ok={data.get('ok')} error={data.get('error')} "
          f"authed_user_keys={list(data.get('authed_user', {}).keys())}")

    if not data.get("ok"):
        err = data.get('error', 'unknown')
        print(f"[slack/callback] OAuth failed: {err}")
        return Response(
            status_code=302,
            headers={"Location": f"/page/?slack_error={err}"},
        )

    authed_user = data.get("authed_user", {})
    token = authed_user.get("access_token")
    if not token:
        print(f"[slack/callback] No user token in response. authed_user={authed_user}")
        return Response(status_code=302, headers={"Location": "/page/?slack_error=no_user_token"})

    team      = data.get("team", {})
    workspace = {
        "team":    team.get("name", "Unknown"),
        "team_id": team.get("id", ""),
        "token":   crypto.encrypt_secret(token),
    }

    with db.lock:
        existing = db.get_settings()
    tokens = [t for t in existing.get("slack_user_tokens", []) if t.get("team_id") != workspace["team_id"]]
    tokens.append(workspace)
    existing["slack_user_tokens"] = tokens

    with db.lock:
        db.save_settings(existing)

    config.apply_overrides(existing)
    return Response(status_code=302, headers={"Location": "/page/?slack_connected=1"})


@app.get("/slack/workspaces")
def get_slack_workspaces():
    """
    Return all connected Slack workspaces (without tokens).

    :return: List of dicts with ``team`` (display name) and ``team_id`` fields.
    :rtype: list[dict]
    """
    with db.lock:
        existing = db.get_settings()
    tokens = existing.get("slack_user_tokens", [])
    return [{"team": t.get("team", "Unknown"), "team_id": t.get("team_id", "")} for t in tokens]


@app.delete("/slack/workspaces/{team_id}")
def disconnect_slack_workspace(team_id: str):
    """
    Remove a Slack workspace's user token from stored settings.

    :param team_id: Slack workspace team ID to disconnect.
    :return: ``{"ok": True}``
    :rtype: dict
    """
    with db.lock:
        existing = db.get_settings()
    tokens = [t for t in existing.get("slack_user_tokens", []) if t.get("team_id") != team_id]
    existing["slack_user_tokens"] = tokens
    with db.lock:
        db.save_settings(existing)
    config.apply_overrides(existing)
    return {"ok": True}


# ── Teams OAuth ────────────────────────────────────────────────────────────────

_TEAMS_REDIRECT_URI = config.TEAMS_REDIRECT_URI
_TEAMS_SCOPES       = "Chat.Read ChannelMessage.Read.All Channel.ReadBasic.All offline_access"


@app.get("/teams/connect")
def teams_connect():
    """
    Begin the Microsoft Teams (Azure AD) OAuth2 user-token flow.

    :return: HTTP 302 redirect to the Microsoft authorization page.
    :raises HTTPException 400: If ``TEAMS_CLIENT_ID`` is not yet configured.
    """
    if not config.TEAMS_CLIENT_ID:
        raise HTTPException(status_code=400, detail="TEAMS_CLIENT_ID not configured — save it in Settings first.")
    state = _new_oauth_state()
    url = (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        f"?client_id={config.TEAMS_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={_TEAMS_REDIRECT_URI}"
        f"&scope={_TEAMS_SCOPES}"
        f"&response_mode=query"
        f"&state={state}"
    )
    return Response(status_code=302, headers={"Location": url})


@app.get("/teams/callback")
def teams_callback(code: str = None, error: str = None, error_description: str = None, state: str = None):
    """
    Handle the Microsoft Teams OAuth2 redirect callback.

    :param code: Authorization code returned by Microsoft.
    :param error: Error identifier returned if the user denied access.
    :param error_description: Human-readable error description.
    :param state: CSRF state nonce generated in ``/teams/connect``.
    :return: HTTP 302 redirect.
    :raises HTTPException 400: If no ``code`` is provided and no ``error`` is set.
    :raises HTTPException 403: If the ``state`` parameter is missing or invalid.
    """
    if error:
        return Response(status_code=302, headers={"Location": f"/page/?teams_error={error}"})
    if not _validate_oauth_state(state):
        raise HTTPException(status_code=403, detail="Invalid or expired OAuth state — possible CSRF attempt.")
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
        "access_token":  crypto.encrypt_secret(access_token),
        "refresh_token": crypto.encrypt_secret(refresh_token or ""),
    }

    with db.lock:
        existing = db.get_settings()
    tokens = [t for t in existing.get("teams_user_tokens", []) if t.get("account_id") != account_id]
    tokens.append(account)
    existing["teams_user_tokens"] = tokens

    with db.lock:
        db.save_settings(existing)

    config.apply_overrides(existing)
    return Response(status_code=302, headers={"Location": "/page/?teams_connected=1"})


@app.get("/teams/workspaces")
def get_teams_workspaces():
    """
    Return all connected Microsoft Teams accounts (without tokens).

    :return: List of dicts with ``display_name``, ``account_id``, and
             ``tenant`` fields.
    :rtype: list[dict]
    """
    with db.lock:
        existing = db.get_settings()
    tokens = existing.get("teams_user_tokens", [])
    return [
        {"display_name": t.get("display_name", "Unknown"), "account_id": t.get("account_id", ""), "tenant": t.get("tenant", "")}
        for t in tokens
    ]


@app.delete("/teams/workspaces/{account_id}")
def disconnect_teams_account(account_id: str):
    """
    Remove a Teams account's token bundle from stored settings.

    :param account_id: Microsoft Graph user ID of the account to disconnect.
    :return: ``{"ok": True}``
    :rtype: dict
    """
    with db.lock:
        existing = db.get_settings()
    tokens = [t for t in existing.get("teams_user_tokens", []) if t.get("account_id") != account_id]
    existing["teams_user_tokens"] = tokens
    with db.lock:
        db.save_settings(existing)
    config.apply_overrides(existing)
    return {"ok": True}


# ── Seed endpoints ─────────────────────────────────────────────────────────────

@app.post("/seed")
async def seed_preview(request: Request):
    """
    Start the seed state machine.  Always succeeds immediately — the
    ``waiting_for_ingest`` phase handles empty databases by polling until
    items arrive.  Returns the current seed job state.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    context = body.get("context", "") if isinstance(body, dict) else ""
    return seeder.start(context)


@app.patch("/seed/context")
async def seed_update_context(request: Request):
    """
    Update the user-provided context string while the seed job is in the
    ``waiting_for_ingest`` state.

    :param request: Request body must be JSON with a ``context`` key.
    :return: ``{"ok": True}``
    :rtype: dict
    """
    body = await request.json()
    seeder.update_context(body.get("context", ""))
    return {"ok": True}


@app.get("/seed/status")
def seed_status():
    """
    Return the current state of the background seed job.

    :return: Current seed job state dict.
    :rtype: dict
    """
    return seeder.status()


@app.post("/seed/apply")
def seed_apply(body: dict, background_tasks: BackgroundTasks):
    """
    Apply the seed editor's confirmed projects and topics to settings.

    :param body: Dict with keys ``projects`` (list), ``topics`` (list), and
                 optionally ``retag`` (bool, default ``True``).
    :return: ``{"ok": True, "projects_added": N, "topics_added": M, "items_retagged": K}``
    :rtype: dict
    """
    return seeder.apply(body, background_tasks)


@app.post("/seed/scan")
def seed_run_scan():
    """
    Transition the seed state machine from ``scan_prompt`` to ``scanning``,
    run a full multi-source scan, then transition to ``done``.

    :return: ``{"ok": True}``
    :raises HTTPException 409: If a scan is already running.
    """
    return seeder.run_scan(scan_state)


@app.post("/seed/skip_scan")
def seed_skip_scan():
    """
    Transition the seed state machine from ``scan_prompt`` to ``done``
    without running a connector scan.

    :return: ``{"ok": True}``
    :rtype: dict
    """
    return seeder.skip_scan()


@app.get("/merllm/status")
def merllm_status():
    """Proxy GET /api/merllm/status from merLLM for the frontend status indicator."""
    try:
        r = http_requests.get(f"{config.MERLLM_URL}/api/merllm/status", timeout=3)
        return r.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "mode": "unknown"}
