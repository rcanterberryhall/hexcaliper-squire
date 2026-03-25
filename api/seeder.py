"""
seeder.py — Seed state machine for bootstrapping project config.

Implements the multi-stage seed workflow that bootstraps project and topic
configuration from an existing corpus using a map-reduce LLM pass:

  start()          — begin the seed job (POST /seed)
  update_context() — update context while waiting for ingest
  status()         — return current job state (GET /seed/status)
  cancel()         — signal the job to stop (POST /analysis/stop)
  apply()          — apply reviewed projects/topics (POST /seed/apply)
  run_scan()       — start a full connector scan (POST /seed/scan)
  skip_scan()      — skip to done without scanning (POST /seed/skip_scan)

All TinyDB table references, db_lock, scan_state, and the Ollama semaphore
are injected via init().  run_scan_fn and run_reanalyze_fn are also injected
so this module does not depend on app.py or orchestrator.py directly.
"""
import json
import threading
from datetime import datetime, timezone

import requests as http_requests

import config
import orchestrator
from agent import extract_keywords

# ── Module-level references, set by init() ────────────────────────────────────

_analyses       = None
_todos          = None
_settings_tbl   = None
_intel_tbl      = None
_situations_tbl = None
_embeddings_tbl = None
_db_lock        = None
_scan_state     = None
_run_scan       = None
_run_reanalyze  = None
_maybe_form_situation = None

_seed_job: dict = {"status": "idle"}


def init(analyses, todos, settings_tbl, intel_tbl, situations_tbl,
         embeddings_tbl, db_lock, scan_state,
         run_scan_fn, run_reanalyze_fn, maybe_form_situation_fn):
    """
    Inject TinyDB table references and shared callables from app.py.

    Must be called once at startup before any seed endpoints are invoked.
    """
    global _analyses, _todos, _settings_tbl, _intel_tbl, _situations_tbl
    global _embeddings_tbl, _db_lock, _scan_state
    global _run_scan, _run_reanalyze, _maybe_form_situation
    _analyses             = analyses
    _todos                = todos
    _settings_tbl         = settings_tbl
    _intel_tbl            = intel_tbl
    _situations_tbl       = situations_tbl
    _embeddings_tbl       = embeddings_tbl
    _db_lock              = db_lock
    _scan_state           = scan_state
    _run_scan             = run_scan_fn
    _run_reanalyze        = run_reanalyze_fn
    _maybe_form_situation = maybe_form_situation_fn


# ── LLM prompts ────────────────────────────────────────────────────────────────

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


# ── Background job ─────────────────────────────────────────────────────────────

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
        seen_items = False
        while True:
            with _db_lock:
                item_count = len(_analyses.all())
            pending = _scan_state.get("ingest_pending", 0)
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
        context = _seed_job.get("context") or context
        context_block = f"Context about {user_name}: {context}" if context else ""
        _seed_job.update({"state": "analyzing", "progress": "Starting analysis…"})

        with _db_lock:
            all_items = _analyses.all()

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

        # Map pass
        map_results  = []
        last_map_err = None
        for batch_num, batch_start in enumerate(range(0, n_items, batch_size), 1):
            if _seed_job.get("cancelled") or _scan_state["cancelled"]:
                _seed_job.update({"state": "idle", "status": "idle", "progress": "Cancelled."})
                return
            _seed_job["progress"] = f"Map pass: batch {batch_num}/{n_batches}…"
            batch = all_items[batch_start:batch_start + batch_size]
            lines = []
            for a in batch:
                source   = a.get("source", "?")
                title    = a.get("title", "(no title)")
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
                with orchestrator.get_sem():
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

        # Reduce pass
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

    except Exception as e:
        _seed_job.update({"state": "error", "status": "error", "progress": str(e)})


# ── Public interface ───────────────────────────────────────────────────────────

def start(context: str) -> dict:
    """
    Start the seed state machine.  Always succeeds immediately.
    Returns the current _seed_job state.
    """
    global _seed_job
    active_states = {"waiting_for_ingest", "analyzing", "reanalyzing", "scanning"}
    if _seed_job.get("state") in active_states:
        return _seed_job

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


def update_context(context: str) -> None:
    """Update the user-provided context string while waiting for ingest."""
    _seed_job["context"] = context


def status() -> dict:
    """Return the current state of the background seed job."""
    return _seed_job


def cancel() -> None:
    """Signal the seed job to stop after the current step."""
    _seed_job["cancelled"] = True


def apply(body: dict, background_tasks) -> dict:
    """
    Apply the seed editor's confirmed projects and topics to settings.

    Steps:
    1. Merges new projects into config.PROJECTS (skipping duplicates).
    2. Merges new topics into config.FOCUS_TOPICS.
    3. Persists updated settings and calls config.apply_overrides.
    4. Optionally retags existing analyses against newly added projects.
    5. Background: embeds tagged items and sweeps situation formation.
    6. Starts _run_reanalyze in a daemon thread and monitors it.

    :param body: Dict with keys ``projects``, ``topics``, ``retag``.
    :param background_tasks: FastAPI BackgroundTasks runner.
    :return: ``{"ok": True, "projects_added": N, "topics_added": M, "items_retagged": K}``
    """
    global _seed_job
    suggested_projects = body.get("projects", [])
    suggested_topics   = body.get("topics", [])
    retag              = body.get("retag", True)

    with _db_lock:
        existing = _settings_tbl.get(doc_id=1) or {}

    current_projects: list[dict] = existing.get("projects", list(config.PROJECTS))
    current_names = {p.get("name", "").lower() for p in current_projects}

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

    existing["projects"]     = current_projects
    existing["focus_topics"] = new_focus_topics
    with _db_lock:
        if _settings_tbl.get(doc_id=1):
            _settings_tbl.update(existing, doc_ids=[1])
        else:
            _settings_tbl.insert(existing)
    config.apply_overrides(existing)

    items_retagged = 0
    if retag and projects_added > 0:
        new_projects = current_projects[-projects_added:]
        with _db_lock:
            all_items = _analyses.all()

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
            with _db_lock:
                for doc_id, tag in updates:
                    _analyses.update({"project_tag": tag}, doc_ids=[doc_id])

    def _seed_embed_and_correlate() -> None:
        """
        Background task run after apply() to warm the embedding and
        situation layers from the existing corpus.
        """
        print("[seed] starting embedding sweep...")
        try:
            from embedder import embed, update_project
            with _db_lock:
                all_items = _analyses.all()
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
            with _db_lock:
                all_items = _analyses.all()
            item_ids = [item.get("item_id") for item in all_items if item.get("item_id")]
            with _db_lock:
                _scan_state["situations_pending"] += len(item_ids)
            for iid in item_ids:
                try:
                    _maybe_form_situation(iid)
                except Exception as e:
                    print(f"[seed] situation sweep {iid}: {e}")
                finally:
                    with _db_lock:
                        _scan_state["situations_pending"] = max(0, _scan_state["situations_pending"] - 1)
            print(f"[seed] situation sweep complete ({len(item_ids)} items)")
        except Exception as e:
            print(f"[seed] situation sweep failed: {e}")

    background_tasks.add_task(_seed_embed_and_correlate)

    _seed_job.update({
        "state":    "reanalyzing",
        "status":   "running",
        "progress": "Re-analyzing all items with new project config…",
    })
    threading.Thread(target=_run_reanalyze, daemon=True).start()

    def _monitor_reanalyze() -> None:
        import time
        time.sleep(1)
        while _scan_state.get("running"):
            _seed_job["progress"] = _scan_state.get("message", "Re-analyzing…")
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


def run_scan(scan_state: dict) -> dict:
    """
    Transition the seed state machine to scanning and start a full connector scan.

    :param scan_state: The shared scan_state dict from app.py.
    :return: ``{"ok": True}``
    :raises fastapi.HTTPException 409: If a scan is already running.
    """
    from fastapi import HTTPException
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


def skip_scan() -> dict:
    """
    Transition the seed state machine from scan_prompt to done without scanning.

    :return: ``{"ok": True}``
    """
    global _seed_job
    _seed_job.update({"state": "done", "status": "done", "progress": "Setup complete."})
    return {"ok": True}
