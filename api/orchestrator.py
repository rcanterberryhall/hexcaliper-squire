"""
orchestrator.py — Scan, reanalyze, and ingest orchestration for Squire.

Owns the Ollama concurrency semaphore and the three background pipeline
functions that drive item analysis:

  run_scan(sources)          — fetch from connectors, analyze, persist
  run_reanalyze()            — re-analyze all stored items with current config
  process_ingest_items(raw)  — analyze a pre-filtered list of new raw items

scan_state and the analysis helper callables are injected via init().
The module-level semaphore (_sem) is always available without init() so it
can be used by seeder.py.

The semaphore is exposed via get_sem() to support future multi-GPU dispatch
without callers needing to access private state.
"""
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests as http_requests

from agent import analyze, build_prompt
from models import RawItem
import connector_slack
import connector_github
import connector_jira
import connector_outlook
import connector_teams
import config
import db
import graph

# ── Ollama concurrency semaphore ───────────────────────────────────────────────
# Set to 1 for single GPU; raise the count for multi-GPU dispatch.
_sem = threading.Semaphore(1)

_TIMING_WINDOW = 10  # rolling average over last N items

CONNECTORS = {
    "slack":   connector_slack,
    "github":  connector_github,
    "jira":    connector_jira,
    "outlook": connector_outlook,
    "teams":   connector_teams,
}

# ── Module-level references, set by init() ────────────────────────────────────

_scan_state:          dict = {}
_save_analysis              = None
_spawn_situation_task       = None
_generate_briefing          = None


def init(scan_state: dict, save_analysis_fn, spawn_situation_fn,
         generate_briefing_fn=None) -> None:
    """
    Inject shared state and callables from app.py.

    Must be called once at startup before any scan or ingest endpoints
    are invoked.
    """
    global _scan_state, _save_analysis, _spawn_situation_task, _generate_briefing
    _scan_state           = scan_state
    _save_analysis        = save_analysis_fn
    _spawn_situation_task = spawn_situation_fn
    _generate_briefing    = generate_briefing_fn


def get_sem() -> threading.Semaphore:
    """Return the shared Ollama concurrency semaphore."""
    return _sem


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── merLLM batch helpers ───────────────────────────────────────────────────────

def _merllm_night_mode() -> bool:
    """Return True if merLLM reports night mode active, False otherwise."""
    try:
        r = http_requests.get(
            f"{config.MERLLM_URL}/api/merllm/status", timeout=5
        )
        r.raise_for_status()
        return r.json().get("mode") == "night"
    except Exception:
        return False


def _submit_batch_job(prompt: str) -> str | None:
    """Submit a prompt to merLLM batch API; return the job ID or None on failure."""
    try:
        r = http_requests.post(
            f"{config.MERLLM_URL}/api/batch/submit",
            json={
                "source_app": "parsival",
                "prompt":     prompt,
                "model":      config.OLLAMA_MODEL,
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("job_id")
    except Exception as e:
        print(f"[batch] submit failed: {e}")
        return None


def _poll_batch_jobs() -> None:
    """
    Background thread: every 60 s poll merLLM for completed batch jobs and
    apply their results to the corresponding items.

    Status handling:
      200            — job complete; parse and apply result
      409 queued/running — still in merLLM queue; keep waiting
      409 failed     — merLLM failed the job; clear batch_job_id so the
                       item is picked up by the next direct reanalyze run
      404            — job unknown to merLLM (e.g. DB wiped); clear and retry
    """
    while True:
        time.sleep(60)
        try:
            with db.lock:
                pending = db.get_items_with_pending_batch()
            for rec in pending:
                job_id = rec.get("batch_job_id")
                if not job_id:
                    continue
                try:
                    r = http_requests.get(
                        f"{config.MERLLM_URL}/api/batch/results/{job_id}",
                        timeout=10,
                    )
                    if r.status_code == 404:
                        # Job unknown — clear so item is retried on next reanalyze
                        print(f"[batch] job {job_id} not found in merLLM — clearing for retry")
                        with db.lock:
                            db.set_batch_job_id(rec["item_id"], None)
                        continue
                    if r.status_code == 409:
                        detail = r.json().get("detail", "")
                        if "failed" in detail:
                            print(f"[batch] job {job_id} failed in merLLM — clearing for retry")
                            with db.lock:
                                db.set_batch_job_id(rec["item_id"], None)
                        # queued or running — still in progress, keep waiting
                        continue
                    r.raise_for_status()
                    data = r.json()
                    response_text = data.get("result", "")
                    if not response_text:
                        continue
                except Exception as e:
                    print(f"[batch] poll {job_id}: {e}")
                    continue

                # Parse the LLM response and re-save the item
                try:
                    import json as _json
                    from agent import (
                        _detect_passdown, _validated_project_tag,
                        _detect_quarantine_noise,
                    )
                    from models import Analysis, ActionItem
                    parsed = _json.loads(response_text)
                    action_items = [
                        ActionItem(
                            description=a.get("description", ""),
                            deadline=a.get("deadline"),
                            owner=a.get("owner", "me"),
                        )
                        for a in parsed.get("action_items", [])
                        if a.get("description")
                    ]
                    category = parsed.get("category", "fyi")
                    if category in ("fyi", "noise"):
                        action_items = []
                    result = Analysis(
                        item_id           = rec["item_id"],
                        source            = rec.get("source", ""),
                        title             = rec.get("title", ""),
                        author            = rec.get("author", ""),
                        timestamp         = rec.get("timestamp", _now_iso()),
                        url               = rec.get("url", ""),
                        category          = category,
                        task_type         = parsed.get("task_type"),
                        has_action        = bool(action_items),
                        priority          = parsed.get("priority", "medium"),
                        action_items      = action_items,
                        summary           = parsed.get("summary", rec.get("title", "")),
                        urgency_reason    = parsed.get("urgency_reason"),
                        hierarchy         = parsed.get("hierarchy", rec.get("hierarchy", "general")),
                        is_passdown       = bool(parsed.get("is_passdown", rec.get("is_passdown", False))),
                        project_tag       = _validated_project_tag(
                                               parsed.get("project_tag") or rec.get("project_tag")
                                           ),
                        direction         = rec.get("direction", "received"),
                        conversation_id   = rec.get("conversation_id"),
                        conversation_topic = rec.get("conversation_topic"),
                        goals             = [g for g in parsed.get("goals", []) if isinstance(g, str) and g],
                        key_dates         = [d for d in parsed.get("key_dates", []) if isinstance(d, dict)],
                        body_preview      = rec.get("body_preview", ""),
                        to_field          = rec.get("to_field", ""),
                        cc_field          = rec.get("cc_field", ""),
                        is_replied        = bool(rec.get("is_replied", False)),
                        replied_at        = rec.get("replied_at"),
                        information_items = [
                            {"fact": i.get("fact", ""), "relevance": i.get("relevance", "")}
                            for i in parsed.get("information_items", [])
                            if i.get("fact")
                        ],
                    )
                    _save_analysis(result, reanalyze=True)
                    graph.index_item(result)
                    _spawn_situation_task(result.item_id)
                    with db.lock:
                        db.set_batch_job_id(rec["item_id"], None)
                    print(f"[batch] applied result for {rec['item_id']} (job {job_id})")
                except Exception as e:
                    print(f"[batch] apply {job_id}: {e}")
        except Exception as e:
            print(f"[batch] poll loop error: {e}")


_batch_poll_thread_started = False


def _ensure_batch_poll_thread() -> None:
    """Start the batch-job polling thread once."""
    global _batch_poll_thread_started
    if not _batch_poll_thread_started:
        _batch_poll_thread_started = True
        threading.Thread(target=_poll_batch_jobs, daemon=True).start()


# ── Pipeline functions ─────────────────────────────────────────────────────────

def _generate_briefing_bg() -> None:
    """
    Call the injected briefing generator in a background thread.

    No-ops silently if ``generate_briefing_fn`` was not provided to ``init()``.
    """
    if not _generate_briefing:
        return
    try:
        content = _generate_briefing()
        with db.lock:
            db.save_briefing(content)
        print(f"[briefing] generated {len(content.get('sections', []))} sections")
    except Exception as e:
        print(f"[briefing] error: {e}")


def run_scan(sources: list[str]) -> None:
    """
    Fetch items from one or more connectors and run LLM analysis on each.

    Iterates ``sources`` in order, calling the matching connector's ``fetch()``
    method.  Each item is then passed to ``agent.analyze`` under ``_sem``
    so only one Ollama call runs at a time.  Saves every result via
    ``_save_analysis`` and spawns a situation-formation task per item.  A scan
    log entry is written regardless of success or cancellation.

    Progress is reflected in the shared ``scan_state`` dict, which the
    frontend polls via ``GET /scan/status``.

    :param sources: List of connector names to fetch from, e.g.
                    ``["slack", "github", "jira"]``.
    :type sources: list[str]
    """
    _scan_state.update({
        "running": True, "cancelled": False, "mode": "scan",
        "progress": 0, "total": 0, "message": "Starting...",
        "total_items": 0, "completed_items": 0,
        "estimated_minutes_remaining": 0,
    })
    started   = _now_iso()
    all_items: list[RawItem] = []

    for src in sources:
        if _scan_state["cancelled"]:
            break
        _scan_state.update({"message": f"Fetching {src}...", "current_source": src})
        connector = CONNECTORS.get(src)
        if connector:
            all_items.extend(connector.fetch())

    _scan_state["total"]       = len(all_items)
    _scan_state["total_items"] = len(all_items)
    _scan_state["message"]     = f"Analyzing {len(all_items)} items..."

    results = []
    _timing: deque = deque(maxlen=_TIMING_WINDOW)
    try:
        for i, item in enumerate(all_items):
            if _scan_state["cancelled"]:
                break
            _scan_state.update({
                "progress":       i,
                "completed_items": i,
                "current_source": item.source,
                "current_item":   item.title[:60],
                "message":        f"[{item.source}] {i + 1}/{len(all_items)}: {item.title[:60]}",
            })
            try:
                _t0 = time.monotonic()
                with _sem:
                    results.append(analyze(item))
                _timing.append(time.monotonic() - _t0)
            except Exception as e:
                print(f"[agent] {item.item_id}: {e}")
            if _timing:
                avg_sec = sum(_timing) / len(_timing)
                remaining = len(all_items) - (i + 1)
                _scan_state["estimated_minutes_remaining"] = round(avg_sec * remaining / 60, 1)

        actions = sum(1 for r in results if r.has_action)
        for r in results:
            _save_analysis(r)
            graph.index_item(r)
            _spawn_situation_task(r.item_id)
        status = "cancelled" if _scan_state["cancelled"] else "success"
        with db.lock:
            db.insert_scan_log({
                "started_at":    started,
                "finished_at":   _now_iso(),
                "sources":       ",".join(sources),
                "items_scanned": len(results),
                "actions_found": actions,
                "status":        status,
            })
        if _scan_state["cancelled"]:
            _scan_state["message"] = (
                f"Stopped — {actions} action items found in "
                f"{len(results)}/{len(all_items)} items processed."
            )
        else:
            _scan_state["message"] = (
                f"Done — {actions} action items found across "
                f"{len(all_items)} items from {', '.join(sources)}. Generating briefing…"
            )
            if results:
                _generate_briefing_bg()
    except Exception as e:
        with db.lock:
            db.insert_scan_log({
                "started_at":    started,
                "finished_at":   _now_iso(),
                "sources":       ",".join(sources),
                "items_scanned": len(results),
                "actions_found": 0,
                "status":        f"error: {e}",
            })
        _scan_state["message"] = f"Error: {e}"
    finally:
        _scan_state["running"]                    = False
        _scan_state["progress"]                   = _scan_state["total"]
        _scan_state["completed_items"]            = _scan_state["total_items"]
        _scan_state["estimated_minutes_remaining"] = 0


def run_reanalyze() -> None:
    """
    Re-run LLM analysis on all stored items using the current config.

    Reconstructs a ``RawItem`` from each stored analysis record (using
    ``body_preview``, ``to_field``, ``cc_field``, and existing ``project_tag``
    as a manual-tag hint), passes it through ``analyze()``, then calls
    ``_save_analysis(reanalyze=True)`` to replace stale todos and intel.
    Situation formation is re-triggered for each item after save.
    Reuses ``scan_state`` for progress reporting.
    """
    _scan_state.update({
        "running": True, "cancelled": False, "mode": "reanalyze",
        "progress": 0, "total": 0, "message": "Loading stored items...",
        "total_items": 0, "completed_items": 0,
        "estimated_minutes_remaining": 0,
    })
    started = _now_iso()
    try:
        with db.lock:
            all_records = db.get_all_items()

        # Priority ordering: user > project > topic > general.
        # Within each tier: passdowns first (richest operational context),
        # then newest items first by timestamp.
        # Python's sort is stable, so three-pass cascade is equivalent to a
        # compound key without needing string negation.
        _HIER_RANK = {"user": 0, "project": 1, "topic": 2, "general": 3}
        all_records.sort(
            key=lambda r: r.get("timestamp") or r.get("processed_at") or "",
            reverse=True,
        )
        all_records.sort(key=lambda r: 0 if r.get("is_passdown") else 1)
        all_records.sort(
            key=lambda r: _HIER_RANK.get(r.get("hierarchy", "general"), 3)
        )

        _scan_state["total"]       = len(all_records)
        _scan_state["total_items"] = len(all_records)
        _scan_state["message"]     = f"Re-analyzing {len(all_records)} items..."

        use_batch = _merllm_night_mode()
        if use_batch:
            print("[reanalyze] night mode active — routing to merLLM batch API")
            _ensure_batch_poll_thread()

        results = []
        _timing: deque = deque(maxlen=_TIMING_WINDOW)
        batch_submitted = 0
        for i, rec in enumerate(all_records):
            if _scan_state["cancelled"]:
                break
            _scan_state.update({
                "progress":       i,
                "completed_items": i,
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
                timestamp = rec.get("timestamp", _now_iso()),
                metadata  = {
                    "to":          rec.get("to_field", ""),
                    "cc":          rec.get("cc_field", ""),
                    "is_replied":  rec.get("is_replied", False),
                    "replied_at":  rec.get("replied_at"),
                    "project_tag": rec.get("project_tag"),
                    "hierarchy":   rec.get("hierarchy"),
                },
            )
            if use_batch:
                try:
                    prompt = build_prompt(item)
                    job_id = _submit_batch_job(prompt)
                    if job_id:
                        with db.lock:
                            db.set_batch_job_id(rec["item_id"], job_id)
                        batch_submitted += 1
                    else:
                        # Batch submit failed; fall back to direct Ollama
                        with _sem:
                            result = analyze(item)
                        results.append(result)
                except Exception as e:
                    print(f"[reanalyze] batch {item.item_id}: {e}")
            else:
                try:
                    _t0 = time.monotonic()
                    with _sem:
                        result = analyze(item)
                    _timing.append(time.monotonic() - _t0)
                    results.append(result)
                except Exception as e:
                    print(f"[reanalyze] {item.item_id}: {e}")
            if _timing:
                avg_sec = sum(_timing) / len(_timing)
                remaining = len(all_records) - (i + 1)
                _scan_state["estimated_minutes_remaining"] = round(avg_sec * remaining / 60, 1)

        actions = sum(1 for r in results if r.has_action)
        for r in results:
            _save_analysis(r, reanalyze=True)
            graph.index_item(r)
            _spawn_situation_task(r.item_id)

        status = "cancelled" if _scan_state["cancelled"] else "success"
        with db.lock:
            db.insert_scan_log({
                "started_at":    started,
                "finished_at":   _now_iso(),
                "sources":       "reanalyze",
                "items_scanned": len(results),
                "actions_found": actions,
                "status":        status,
            })
        if use_batch and batch_submitted:
            _scan_state["message"] = (
                f"Re-analysis queued — {batch_submitted} items sent to batch, "
                f"{len(results)} processed directly. Results apply automatically."
            )
        else:
            _scan_state["message"] = (
                f"Re-analysis complete — {len(results)} items processed, "
                f"{actions} action items found. Generating briefing…"
            )
        if results:
            _generate_briefing_bg()
    except Exception as e:
        _scan_state["message"] = f"Re-analysis error: {e}"
        print(f"[reanalyze] {e}")
    finally:
        _scan_state["running"]                    = False
        _scan_state["progress"]                   = _scan_state["total"]
        _scan_state["completed_items"]            = _scan_state["total_items"]
        _scan_state["estimated_minutes_remaining"] = 0


def process_ingest_items(raw: list[RawItem]) -> None:
    """
    Analyse a pre-filtered list of new raw items from the ingest endpoint.

    Called as a FastAPI background task.  Respects ``scan_state["cancelled"]``
    and tracks in-flight count via ``scan_state["ingest_pending"]``.

    :param raw: List of deduplicated ``RawItem`` objects to analyse.
    :type raw: list[RawItem]
    """
    with db.lock:
        _scan_state["ingest_pending"] += len(raw)
    for item in raw:
        if _scan_state["cancelled"]:
            with db.lock:
                _scan_state["ingest_pending"] = 0
            print("[ingest] cancelled — stopping after current item")
            return
        try:
            with _sem:
                result = analyze(item)
            _save_analysis(result)
            graph.index_item(result)
            _spawn_situation_task(result.item_id)
        except Exception as e:
            print(f"[ingest] {item.item_id}: {e}")
        with db.lock:
            _scan_state["ingest_pending"] = max(0, _scan_state["ingest_pending"] - 1)
