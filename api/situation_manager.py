"""
situation_manager.py — Situation formation, scoring, and response helpers.

Owns all situation-lifecycle logic extracted from app.py:
  _maybe_form_situation()    — correlate an item and form or merge situations
  _spawn_situation_task()    — daemon-thread wrapper for _maybe_form_situation
  _sync_situation_tags_*()   — keep situation project_tag in sync with members
  _update_situation_record() — full LLM re-synthesis for an existing situation
  _rescore_situation()       — score-only refresh (no LLM)
  _rescore_all_situations()  — sweep all non-dismissed situations
  _situation_response()      — build the API response dict for a situation
  _score_decay_loop()        — 30-minute daemon that decays situation scores

All TinyDB table references and db_lock are injected via init() to avoid
circular imports with app.py.  scan_state is also injected so situation
formation can check the cancellation flag.
"""
import json
import threading
import uuid as _uuid
from datetime import datetime, timezone

from tinydb import Query

import config
import correlator as _correlator

# ── Module-level references, set by init() ────────────────────────────────────

_analyses       = None
_situations_tbl = None
_intel_tbl      = None
_db_lock        = None
_scan_state     = None

Q = Query()


def init(analyses, situations_tbl, intel_tbl, db_lock, scan_state):
    """
    Inject TinyDB table references and shared state from app.py.

    Must be called once at startup, immediately after the TinyDB tables are
    created, before any route handlers execute.  Starts the 30-minute score
    decay daemon thread.
    """
    global _analyses, _situations_tbl, _intel_tbl, _db_lock, _scan_state
    _analyses       = analyses
    _situations_tbl = situations_tbl
    _intel_tbl      = intel_tbl
    _db_lock        = db_lock
    _scan_state     = scan_state
    threading.Thread(target=_score_decay_loop, daemon=True).start()


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ── Priority helper ────────────────────────────────────────────────────────────

def _pri_rank(p: str) -> int:
    """
    Convert a priority string to a numeric rank for comparison.

    :param p: Priority string — ``"high"``, ``"medium"``, or ``"low"``.
    :type p: str
    :return: Numeric rank: high=3, medium=2, low=1.  Unknown values return 1.
    :rtype: int
    """
    return {"high": 3, "medium": 2, "low": 1}.get(p, 1)


# ── Score decay ────────────────────────────────────────────────────────────────

def _rescore_situation(sit_id: str) -> None:
    """
    Recompute only the urgency score for a single situation without re-running LLM synthesis.

    Cheaper than ``_update_situation_record`` — used by the 30-minute decay
    loop (``_score_decay_loop``) and when a newly saved item correlates with
    an existing situation but is not merged (i.e. it was already a member).

    :param sit_id: UUID of the situation to rescore.
    :type sit_id: str
    """
    try:
        with _db_lock:
            sit = _situations_tbl.get(Q.situation_id == sit_id)
        if not sit:
            return
        item_ids = sit.get("item_ids", [])
        with _db_lock:
            cluster_records = [_analyses.get(Q.item_id == iid) for iid in item_ids]
        cluster_records = [r for r in cluster_records if r]
        if not cluster_records:
            return
        score = _correlator.score_situation(item_ids, cluster_records)
        with _db_lock:
            _situations_tbl.update(
                {"score": score, "score_updated_at": now_iso()},
                Q.situation_id == sit_id,
            )
    except Exception as e:
        print(f"[correlator] _rescore_situation({sit_id}): {e}")


def _rescore_all_situations() -> None:
    """
    Recompute urgency scores for all non-dismissed situations.

    Iterates every situation in ``situations_tbl`` that is not marked
    ``dismissed`` and calls ``_rescore_situation`` on each.  Exceptions for
    individual situations are caught and logged so a single failure does not
    abort the sweep.

    Called every 30 minutes by the daemon thread started at module load time
    (``_score_decay_loop``).
    """
    with _db_lock:
        sit_ids = [s["situation_id"] for s in _situations_tbl.all() if not s.get("dismissed")]
    for sid in sit_ids:
        try:
            _rescore_situation(sid)
        except Exception as e:
            print(f"[score_decay] {sid}: {e}")


def _score_decay_loop():
    """Recompute situation scores every 30 minutes to reflect recency decay."""
    import time
    while True:
        time.sleep(1800)
        try:
            _rescore_all_situations()
        except Exception as e:
            print(f"[score_decay] {e}")


# ── Situation record management ────────────────────────────────────────────────

def _update_situation_record(sit_id: str, item_ids: list) -> None:
    """
    Reload cluster records and recompute score and LLM synthesis for an existing situation.

    Fetches all analysis records for ``item_ids``, re-runs
    ``correlator.synthesize_situation`` (full LLM pass) and
    ``correlator.score_situation``, then writes the updated fields back to
    ``situations_tbl``.  Also re-links every item in ``item_ids`` to
    ``sit_id`` via ``analyses.update`` in case new items were merged in.

    Called when a new item is merged into an existing situation and after
    ``POST /situations/{situation_id}/rescore``.

    :param sit_id: UUID of the situation to update.
    :type sit_id: str
    :param item_ids: Complete ordered list of analysis item IDs for this situation.
    :type item_ids: list
    """
    try:
        with _db_lock:
            cluster_records = [_analyses.get(Q.item_id == iid) for iid in item_ids]
        cluster_records = [r for r in cluster_records if r]
        if not cluster_records:
            return

        with _db_lock:
            cluster_intel = [
                i for i in _intel_tbl.all()
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
        with _db_lock:
            _situations_tbl.update(updates, Q.situation_id == sit_id)
            for iid in item_ids:
                _analyses.update({"situation_id": sit_id}, Q.item_id == iid)
    except Exception as e:
        print(f"[correlator] _update_situation_record({sit_id}): {e}")


def _sync_situation_tags_for_item(item_id: str) -> None:
    """
    Recompute ``project_tag`` on every situation that contains ``item_id``.

    Lightweight — no LLM call.  Mirrors the consensus rule used in
    ``_update_situation_record``: set the tag only when *all* member analyses
    agree on the same non-null tag, otherwise set it to ``None``.

    Called synchronously after any endpoint that changes a single analysis's
    ``project_tag``.

    :param item_id: Stable ID of the analysis whose tag just changed.
    :type item_id: str
    """
    with _db_lock:
        affected = [s for s in _situations_tbl.all() if item_id in s.get("item_ids", [])]
    for sit in affected:
        sit_id   = sit.get("situation_id")
        mem_ids  = sit.get("item_ids", [])
        with _db_lock:
            members = [_analyses.get(Q.item_id == iid) for iid in mem_ids]
        members   = [m for m in members if m]
        proj_tags = list({m.get("project_tag") for m in members if m.get("project_tag")})
        new_tag   = proj_tags[0] if len(proj_tags) == 1 else None
        if new_tag != sit.get("project_tag"):
            with _db_lock:
                _situations_tbl.update({"project_tag": new_tag}, Q.situation_id == sit_id)


def _sync_situation_tags_all() -> None:
    """
    Recompute ``project_tag`` on every situation.

    Called after a bulk project-tag change (e.g. project deletion in
    ``save_settings``).  Uses the same consensus rule as
    ``_sync_situation_tags_for_item``.
    """
    with _db_lock:
        all_sits = _situations_tbl.all()
    for sit in all_sits:
        sit_id   = sit.get("situation_id")
        mem_ids  = sit.get("item_ids", [])
        with _db_lock:
            members = [_analyses.get(Q.item_id == iid) for iid in mem_ids]
        members   = [m for m in members if m]
        proj_tags = list({m.get("project_tag") for m in members if m.get("project_tag")})
        new_tag   = proj_tags[0] if len(proj_tags) == 1 else None
        if new_tag != sit.get("project_tag"):
            with _db_lock:
                _situations_tbl.update({"project_tag": new_tag}, Q.situation_id == sit_id)


# ── Formation ──────────────────────────────────────────────────────────────────

def _maybe_form_situation(item_id: str) -> None:
    """
    Attempt to correlate a newly saved item with existing analyses and form
    or update a ``Situation`` record.

    Algorithm:
    1. Load the item's stored record and parse its cross-source reference
       tokens (e.g. Jira keys, PR numbers).
    2. Retrieve the item's embedding vector (if available).
    3. Call ``correlator.find_correlated_candidates`` with references, vector,
       and project tag to identify related items.
    4. If no candidates are found, rescore the item's existing situation (if
       any) and return.
    5. If any candidate already belongs to a situation, merge this item (and
       any additional orphan situations) into the highest-scoring one via
       ``_update_situation_record``.
    6. Otherwise, if the cluster meets the minimum size (≥ 2), create a new
       ``Situation`` document via ``correlator.synthesize_situation`` and
       ``correlator.score_situation``, then link all cluster items.

    All TinyDB writes are serialised through ``_db_lock``.  Exceptions are
    caught and logged so a single failing item does not block the ingest queue.

    :param item_id: Stable ID of the analysis item to process.
    :type item_id: str
    """
    try:
        from embedder import get_item_vector

        with _db_lock:
            record       = _analyses.get(Q.item_id == item_id)
            all_analyses = _analyses.all()
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
        with _db_lock:
            sit_ids = set()
            for cid in candidates:
                r = _analyses.get(Q.item_id == cid)
                if r and r.get("situation_id"):
                    sit_ids.add(r["situation_id"])

        if sit_ids:
            # Merge into the highest-scoring existing situation
            target_sit_id = sit_ids.pop()
            with _db_lock:
                sit = _situations_tbl.get(Q.situation_id == target_sit_id)
            if sit:
                updated_ids = list(dict.fromkeys(sit.get("item_ids", []) + [item_id]))
                # Merge any additional sit_ids
                for extra_sid in sit_ids:
                    with _db_lock:
                        extra = _situations_tbl.get(Q.situation_id == extra_sid)
                    if extra:
                        updated_ids = list(dict.fromkeys(updated_ids + extra.get("item_ids", [])))
                        with _db_lock:
                            _situations_tbl.remove(Q.situation_id == extra_sid)

                _update_situation_record(target_sit_id, updated_ids)
                with _db_lock:
                    _analyses.update({"situation_id": target_sit_id}, Q.item_id == item_id)
                return

        # No existing situation — check minimum cluster requirements
        all_ids = [item_id] + candidates
        with _db_lock:
            cluster_records = [_analyses.get(Q.item_id == iid) for iid in all_ids]
        cluster_records = [r for r in cluster_records if r]

        if len(cluster_records) < 2:
            return

        if _scan_state["cancelled"]:
            return

        # Create new situation
        with _db_lock:
            cluster_intel = [
                i for i in _intel_tbl.all()
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
        with _db_lock:
            _situations_tbl.insert(sit_doc)
            for iid in all_ids:
                _analyses.update({"situation_id": sit_id}, Q.item_id == iid)

        print(f"[correlator] formed situation {sit_id[:8]} from {len(all_ids)} items")

    except Exception as e:
        print(f"[correlator] _maybe_form_situation({item_id}): {e}")


def _spawn_situation_task(item_id: str) -> None:
    """
    Spawn ``_maybe_form_situation`` in a daemon thread and track it in ``scan_state``.

    Increments ``_scan_state["situations_pending"]`` before starting the thread
    and decrements it in a ``finally`` block so the counter stays accurate even
    when situation formation raises an exception.

    :param item_id: Stable ID of the analysis item to process.
    :type item_id: str
    """
    with _db_lock:
        _scan_state["situations_pending"] += 1

    def _run() -> None:
        try:
            _maybe_form_situation(item_id)
        finally:
            with _db_lock:
                _scan_state["situations_pending"] = max(0, _scan_state["situations_pending"] - 1)

    threading.Thread(target=_run, daemon=True).start()


# ── Response builder ───────────────────────────────────────────────────────────

def _situation_response(sit: dict) -> dict:
    """
    Build the API response dict for a situation.

    Fetches a lightweight summary (``item_id``, ``source``, ``title``,
    ``priority``, ``timestamp``) for each contributing analysis and includes
    it in the response as the ``items`` list.

    :param sit: Raw situation document dict from TinyDB.
    :type sit: dict
    :return: API-ready dict with all situation fields plus lightweight ``items``.
    :rtype: dict
    """
    item_ids = sit.get("item_ids", [])
    with _db_lock:
        items_raw = [_analyses.get(Q.item_id == iid) for iid in item_ids]
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
