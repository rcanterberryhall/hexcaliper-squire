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
from datetime import datetime, timezone

from agent import analyze
from models import RawItem
import connector_slack
import connector_github
import connector_jira
import connector_outlook
import connector_teams
import db
import graph

# ── Ollama concurrency semaphore ───────────────────────────────────────────────
# Set to 1 for single GPU; raise the count for multi-GPU dispatch.
_sem = threading.Semaphore(1)

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

    _scan_state["total"]   = len(all_items)
    _scan_state["message"] = f"Analyzing {len(all_items)} items..."

    results = []
    try:
        for i, item in enumerate(all_items):
            if _scan_state["cancelled"]:
                break
            _scan_state.update({
                "progress":       i,
                "current_source": item.source,
                "current_item":   item.title[:60],
                "message":        f"[{item.source}] {i + 1}/{len(all_items)}: {item.title[:60]}",
            })
            try:
                with _sem:
                    results.append(analyze(item))
            except Exception as e:
                print(f"[agent] {item.item_id}: {e}")

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
        _scan_state["running"]  = False
        _scan_state["progress"] = _scan_state["total"]


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
    })
    started = _now_iso()
    try:
        with db.lock:
            all_records = db.get_all_items()

        # Process passdowns first — they are the richest source of operational
        # current state and their intel should be available when situations
        # form for subsequent items.
        all_records.sort(key=lambda r: (0 if r.get("is_passdown") else 1, r.get("timestamp", "")))

        _scan_state["total"]   = len(all_records)
        _scan_state["message"] = f"Re-analyzing {len(all_records)} items..."

        results = []
        for i, rec in enumerate(all_records):
            if _scan_state["cancelled"]:
                break
            _scan_state.update({
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
            try:
                with _sem:
                    result = analyze(item)
                results.append(result)
            except Exception as e:
                print(f"[reanalyze] {item.item_id}: {e}")

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
        _scan_state["running"]  = False
        _scan_state["progress"] = _scan_state["total"]


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
