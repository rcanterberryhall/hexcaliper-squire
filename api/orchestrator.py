"""
orchestrator.py — Scan, reanalyze, and ingest orchestration for Squire.

Owns the three background pipeline functions that drive item analysis:

  run_scan(sources)          — fetch from connectors, analyze, persist
  run_reanalyze()            — re-analyze all stored items with current config
  process_ingest_items(raw)  — analyze a pre-filtered list of new raw items

scan_state and the analysis helper callables are injected via init().

GPU concurrency note (squire#33)
--------------------------------
This module used to wrap every ``analyze()`` call in a
``threading.Semaphore(1)`` named ``_sem``, dating from the single-GPU era
when parsival talked directly to Ollama.  Every parsival LLM call now goes
through merLLM, which round-robins across all available GPUs and runs its
own unified tracked queue.  Layering a parsival-side throttle on top of
that is strictly subtractive — the lower of the two concurrency limits
wins, and ours was hard-coded to 1, silently halving sync-path throughput.

The architectural rule is therefore:

    merLLM is the single source of truth for GPU concurrency.
    parsival never gates LLM traffic on its own.

If we ever need backpressure from merLLM, the right place is merLLM
returning 429s (or its tracked queue blocking) — not a parsival-side
pre-throttle that has to be kept in sync by hand every time the GPU
topology changes.
"""
import threading
import time
from collections import deque
from datetime import datetime, timezone

import logging
import requests as http_requests

log = logging.getLogger(__name__)

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
import noise_filter as _nf

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Noise filter helpers ───────────────────────────────────────────────────────

def _get_noise_rules() -> list[dict]:
    """Return the current noise filter rules from settings."""
    with db.lock:
        settings = db.get_settings() or {}
    return settings.get("noise_filters", [])


def _save_filtered_item(item: RawItem, matched_rule: str) -> None:
    """
    Persist a filtered item to the items table as category='filtered'.
    No LLM analysis is run; the item is auditable but invisible in the normal UI.
    """
    with db.lock:
        existing = db.get_item(item.item_id)
    if existing:
        return  # already stored (possibly from a previous scan)
    with db.lock:
        db.upsert_item({
            "item_id":         item.item_id,
            "source":          item.source,
            "title":           item.title,
            "author":          item.author,
            "timestamp":       item.timestamp,
            "url":             item.url,
            "has_action":      0,
            "priority":        "low",
            "category":        "filtered",
            "summary":         f"[filtered by {matched_rule}]",
            "urgency":         None,
            "action_items":    "[]",
            "processed_at":    _now_iso(),
        })


# ── merLLM batch helpers ───────────────────────────────────────────────────────

def _merllm_batch_available() -> bool:
    """Return True if merLLM is reachable and can accept batch jobs."""
    try:
        r = http_requests.get(
            f"{config.MERLLM_URL}/api/merllm/status", timeout=5
        )
        r.raise_for_status()
        return True
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
                "model":      config.effective_model(),
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("job_id")
    except Exception as e:
        log.error("batch submit failed: %s", e)
        return None


def _raw_item_from_record(rec: dict) -> RawItem:
    """
    Reconstruct a minimal ``RawItem`` from a stored DB record.

    Used by the batch poll path so it can feed the same
    :func:`agent.build_analysis_from_llm_json` helper that the sync path uses.
    The body is taken from the already-stripped ``body_preview`` (idempotent
    under ``_strip_caution``), and metadata is populated with every field the
    helper reads.
    """
    return RawItem(
        source    = rec.get("source", "") or "",
        item_id   = rec["item_id"],
        title     = rec.get("title", "") or "",
        body      = rec.get("body_preview", "") or "",
        url       = rec.get("url", "") or "",
        author    = rec.get("author", "") or "",
        timestamp = rec.get("timestamp") or _now_iso(),
        metadata  = {
            "to":                 rec.get("to_field", "") or "",
            "cc":                 rec.get("cc_field", "") or "",
            "is_replied":         bool(rec.get("is_replied", False)),
            "replied_at":         rec.get("replied_at"),
            "hierarchy":          rec.get("hierarchy", "general") or "general",
            "direction":          rec.get("direction", "received") or "received",
            "conversation_id":    rec.get("conversation_id"),
            "conversation_topic": rec.get("conversation_topic"),
            "project_tag":        rec.get("project_tag"),
        },
    )


def _apply_batch_result(rec: dict, response_text: str) -> None:
    """
    Apply a completed batch LLM result to a stored item.

    Parses the LLM JSON via :func:`agent.build_analysis_from_llm_json`,
    re-saves the analysis, indexes it into the graph, spawns the situation
    task, and clears ``batch_job_id``.

    Raised exceptions are caller's responsibility — the caller decides whether
    to clear ``batch_job_id`` to unstick the item or leave it for retry.
    """
    from agent import build_analysis_from_llm_json, compute_recipient_scope

    raw = _raw_item_from_record(rec)
    scope_info = compute_recipient_scope(
        config.USER_EMAIL or "",
        raw.metadata.get("to", ""),
        raw.metadata.get("cc", ""),
    )
    result = build_analysis_from_llm_json(raw, response_text, scope_info=scope_info)

    _save_analysis(result, reanalyze=True)
    graph.index_item(result)
    _spawn_situation_task(result.item_id)
    with db.lock:
        db.set_batch_job_id(rec["item_id"], None)


def _poll_batch_once() -> None:
    """
    One iteration of the batch poll loop.  Extracted from
    :func:`_poll_batch_jobs` so tests can drive it directly without spawning
    a thread.

    Status handling:
      200            — job complete; parse and apply result
      409 queued/running — still in merLLM queue; keep waiting
      409 failed     — merLLM failed the job; clear batch_job_id so the
                       item is picked up by the next direct reanalyze run
      404            — job unknown to merLLM (e.g. DB wiped); clear and retry

    A parse/apply failure clears ``batch_job_id`` so a malformed result does
    not wedge the item forever.  The next reanalyze run will pick it up.
    """
    with db.lock:
        pending = db.get_items_with_pending_batch()

    for rec in pending:
        job_id = rec.get("batch_job_id")
        if not job_id:
            continue

        # ── Fetch result from merLLM ──────────────────────────────────────
        try:
            r = http_requests.get(
                f"{config.MERLLM_URL}/api/batch/results/{job_id}",
                timeout=10,
            )
            if r.status_code == 404:
                log.warning("batch job %s not found in merLLM — clearing for retry", job_id)
                with db.lock:
                    db.set_batch_job_id(rec["item_id"], None)
                continue
            if r.status_code == 409:
                detail = r.json().get("detail", "")
                if "failed" in detail:
                    log.warning("batch job %s failed in merLLM — clearing for retry", job_id)
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
            log.error("batch poll %s: %s", job_id, e)
            continue

        # ── Parse and apply via shared helper ─────────────────────────────
        try:
            _apply_batch_result(rec, response_text)
            log.info("batch applied result for %s (job %s)", rec["item_id"], job_id)
        except Exception as e:
            # Clear the job id so a malformed payload does not wedge the item
            # forever — the next reanalyze run will pick it up.
            log.error("batch apply %s: %s — clearing job id to unstick item", job_id, e)
            try:
                with db.lock:
                    db.set_batch_job_id(rec["item_id"], None)
            except Exception as clear_err:
                log.error("failed to clear stuck batch_job_id for %s: %s", rec["item_id"], clear_err)


def _poll_batch_jobs() -> None:
    """
    Background thread: every 60 s call :func:`_poll_batch_once` to poll
    merLLM for completed batch jobs and apply their results.
    """
    while True:
        time.sleep(60)
        try:
            _poll_batch_once()
        except Exception as e:
            log.error("batch poll loop error: %s", e)


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
        log.info("briefing generated %d sections", len(content.get('sections', [])))
    except Exception as e:
        log.error("briefing error: %s", e)


def run_scan(sources: list[str]) -> None:
    """
    Fetch items from one or more connectors and run LLM analysis on each.

    Iterates ``sources`` in order, calling the matching connector's ``fetch()``
    method.  Each item is then passed to ``agent.analyze``; concurrency
    against the LLM is owned entirely by merLLM (see the module docstring
    for the squire#33 rationale).  Saves every result via ``_save_analysis``
    and spawns a situation-formation task per item.  A scan log entry is
    written regardless of success or cancellation.

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
    noise_rules    = _get_noise_rules()
    filtered_count = 0
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
            matched, rule_type = _nf.should_filter(item, noise_rules)
            if matched:
                _save_filtered_item(item, rule_type)
                filtered_count += 1
                continue
            try:
                _t0 = time.monotonic()
                results.append(analyze(item, priority="short"))
                _timing.append(time.monotonic() - _t0)
            except Exception as e:
                log.error("agent %s: %s", item.item_id, e)
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
        filter_note = f", {filtered_count} filtered" if filtered_count else ""
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
                f"{len(results)}/{len(all_items)} items processed{filter_note}."
            )
        else:
            _scan_state["message"] = (
                f"Done — {actions} action items found across "
                f"{len(all_items)} items from {', '.join(sources)}{filter_note}. Generating briefing…"
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
        # Clear the cancellation flag so a subsequent ingest/scan/reanalyze
        # is not poisoned by a cancel that belonged to this run.
        # process_ingest_items shares _scan_state and aborts if it sees
        # cancelled=True, so leaking the flag silently breaks the next
        # fresh-item analyze path.
        _scan_state["cancelled"]                  = False
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

        use_batch = _merllm_batch_available()
        if use_batch:
            log.info("reanalyze: merLLM available — routing to batch API")
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
                        # Batch submit failed; fall back to direct merLLM call.
                        # Concurrency against the LLM is owned by merLLM —
                        # see module docstring (squire#33).  Priority is
                        # ``background`` so bulk reanalysis cannot starve
                        # chat or fresh ingest (squire#34).
                        result = analyze(item, priority="background")
                        results.append(result)
                except Exception as e:
                    log.error("reanalyze batch %s: %s", item.item_id, e)
            else:
                try:
                    _t0 = time.monotonic()
                    result = analyze(item, priority="background")
                    _timing.append(time.monotonic() - _t0)
                    results.append(result)
                except Exception as e:
                    log.error("reanalyze %s: %s", item.item_id, e)
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
        log.error("reanalyze: %s", e)
    finally:
        _scan_state["running"]                    = False
        # Clear the cancellation flag so a subsequent ingest/scan/reanalyze
        # is not poisoned by a cancel that belonged to this run. See the
        # matching comment in run_scan() for the full rationale.
        _scan_state["cancelled"]                  = False
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
    noise_rules = _get_noise_rules()
    with db.lock:
        _scan_state["ingest_pending"] += len(raw)
    for item in raw:
        if _scan_state["cancelled"]:
            with db.lock:
                _scan_state["ingest_pending"] = 0
            log.info("ingest cancelled — stopping after current item")
            return
        matched, rule_type = _nf.should_filter(item, noise_rules)
        if matched:
            _save_filtered_item(item, rule_type)
            with db.lock:
                _scan_state["ingest_pending"] = max(0, _scan_state["ingest_pending"] - 1)
            continue
        try:
            result = analyze(item, priority="short")
            _save_analysis(result)
            graph.index_item(result)
            _spawn_situation_task(result.item_id)
        except Exception as e:
            log.error("ingest %s: %s", item.item_id, e)
        with db.lock:
            _scan_state["ingest_pending"] = max(0, _scan_state["ingest_pending"] - 1)


# ── Auto-scan scheduler ────────────────────────────────────────────────────────
# Manages per-source repeating timers.  Each enabled source fires run_scan()
# on its own interval, skipping if a scan is already in progress.

import logging as _logging
_sched_log = _logging.getLogger("parsival.scheduler")

# {source: {"interval_min": int, "next_run": float|None, "last_run": float|None, "timer": Timer|None}}
_schedule: dict = {}
_schedule_lock = threading.Lock()

SCHEDULABLE_SOURCES = list(CONNECTORS.keys())   # ["slack","github","jira","outlook","teams"]


def _fire_auto_scan(source: str) -> None:
    """Execute one auto-scan for source, then re-arm the timer."""
    with _schedule_lock:
        entry = _schedule.get(source)
        if not entry or entry["interval_min"] <= 0:
            return

    if _scan_state.get("running"):
        _sched_log.info("auto-scan %s: skipped — scan already running", source)
    else:
        _sched_log.info("auto-scan %s: starting", source)
        try:
            run_scan([source])
        except Exception as exc:
            _sched_log.error("auto-scan %s: error — %s", source, exc)

    with _schedule_lock:
        entry = _schedule.get(source)
        if not entry or entry["interval_min"] <= 0:
            return
        entry["last_run"] = time.time()
        interval_sec = entry["interval_min"] * 60
        entry["next_run"] = time.time() + interval_sec
        t = threading.Timer(interval_sec, _fire_auto_scan, args=(source,))
        t.daemon = True
        t.start()
        entry["timer"] = t


def scheduler_update(schedule_dict: dict) -> None:
    """
    Apply a new scan schedule.

    ``schedule_dict`` maps source names to interval minutes (0 = disabled).
    Existing timers are cancelled; new ones are armed for non-zero intervals.

    Example: ``{"slack": 30, "github": 60, "jira": 0, "outlook": 0, "teams": 0}``
    """
    with _schedule_lock:
        # Cancel all existing timers.
        for entry in _schedule.values():
            t = entry.get("timer")
            if t:
                t.cancel()
        _schedule.clear()

    for source, interval_min in schedule_dict.items():
        if source not in SCHEDULABLE_SOURCES:
            continue
        interval_min = int(interval_min or 0)
        with _schedule_lock:
            _schedule[source] = {
                "interval_min": interval_min,
                "next_run":     None,
                "last_run":     None,
                "timer":        None,
            }
        if interval_min > 0:
            interval_sec = interval_min * 60
            with _schedule_lock:
                _schedule[source]["next_run"] = time.time() + interval_sec
            t = threading.Timer(interval_sec, _fire_auto_scan, args=(source,))
            t.daemon = True
            t.start()
            with _schedule_lock:
                _schedule[source]["timer"] = t
            _sched_log.info("auto-scan %s: armed every %d min", source, interval_min)


def get_schedule_status() -> dict:
    """Return per-source schedule status for GET /scan/status."""
    with _schedule_lock:
        result = {}
        for source, entry in _schedule.items():
            next_run = entry.get("next_run")
            last_run = entry.get("last_run")
            result[source] = {
                "interval_min": entry["interval_min"],
                "next_run":     datetime.fromtimestamp(next_run, tz=timezone.utc).isoformat() if next_run else None,
                "last_run":     datetime.fromtimestamp(last_run, tz=timezone.utc).isoformat() if last_run else None,
            }
        return result
