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

Uses db.py directly for all persistence.  scan_state is injected via init()
so situation formation can check the cancellation flag.
"""
import json
import logging
import threading
import uuid as _uuid
from datetime import datetime, timezone

import config
import correlator as _correlator

log = logging.getLogger(__name__)
import db

# ── Module-level references, set by init() ────────────────────────────────────

_scan_state: dict = {}


def init(scan_state: dict) -> None:
    """
    Inject shared scan state from app.py and start the score-decay daemon.

    Must be called once at startup before any route handlers execute.
    """
    global _scan_state
    _scan_state = scan_state
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
        with db.lock:
            sit = db.get_situation(sit_id)
        if not sit:
            return
        item_ids = sit.get("item_ids", [])
        with db.lock:
            cluster_records = [db.get_item(iid) for iid in item_ids]
        cluster_records = [r for r in cluster_records if r]
        if not cluster_records:
            return
        score = _correlator.score_situation(item_ids, cluster_records)
        with db.lock:
            db.update_situation(sit_id, {"score": score, "score_updated_at": now_iso()})
    except Exception as e:
        log.error("_rescore_situation(%s): %s", sit_id, e)


def _rescore_all_situations() -> None:
    """
    Recompute urgency scores for all non-dismissed situations.

    Iterates every situation that is not marked ``dismissed`` and calls
    ``_rescore_situation`` on each.  Exceptions for individual situations are
    caught and logged so a single failure does not abort the sweep.

    Called every 30 minutes by the daemon thread started at module load time
    (``_score_decay_loop``).
    """
    with db.lock:
        sit_ids = [s["situation_id"] for s in db.get_all_situations(include_dismissed=False)]
    for sid in sit_ids:
        try:
            _rescore_situation(sid)
        except Exception as e:
            log.error("score_decay %s: %s", sid, e)


def _score_decay_loop():
    """Recompute situation scores every 30 minutes to reflect recency decay."""
    import time
    while True:
        time.sleep(1800)
        try:
            _rescore_all_situations()
        except Exception as e:
            log.error("score_decay: %s", e)


# ── Situation record management ────────────────────────────────────────────────

def _update_situation_record(sit_id: str, item_ids: list) -> None:
    """
    Reload cluster records and recompute score and LLM synthesis for an existing situation.

    Fetches all analysis records for ``item_ids``, re-runs
    ``correlator.synthesize_situation`` (full LLM pass) and
    ``correlator.score_situation``, then writes the updated fields back to the
    situations table.  Also re-links every item in ``item_ids`` to ``sit_id``
    in case new items were merged in.

    Called when a new item is merged into an existing situation and after
    ``POST /situations/{situation_id}/rescore``.

    :param sit_id: UUID of the situation to update.
    :type sit_id: str
    :param item_ids: Complete ordered list of analysis item IDs for this situation.
    :type item_ids: list
    """
    try:
        with db.lock:
            cluster_records = [db.get_item(iid) for iid in item_ids]
        cluster_records = [r for r in cluster_records if r]
        if not cluster_records:
            return

        with db.lock:
            cluster_intel = db.get_intel_for_items(list(set(item_ids)))
        synthesis   = _correlator.synthesize_situation(cluster_records, config.USER_NAME or "the user", intel_items=cluster_intel)
        score       = _correlator.score_situation(item_ids, cluster_records)
        max_pri     = max(cluster_records, key=lambda r: _pri_rank(r.get("priority", "low")))
        all_refs    = list(set(r for rec in cluster_records
                               for raw in [rec.get("references")]
                               for r in (json.loads(raw) if isinstance(raw, str) and raw else (raw or []))))
        sources     = list(set(r.get("source", "") for r in cluster_records))
        last_ts     = max((r.get("timestamp", "") for r in cluster_records), default=now_iso())
        # Collect all project tags across cluster members (handles multi-tag)
        all_proj = set()
        for r in cluster_records:
            all_proj.update(db.parse_project_tags(r.get("project_tag")))
        proj_tag_val = db.serialize_project_tags(sorted(all_proj)) if all_proj else None

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
            "project_tag":      proj_tag_val,
            "score_updated_at": now_iso(),
        }
        with db.lock:
            db.update_situation(sit_id, updates)
            for iid in item_ids:
                db.update_item(iid, {"situation_id": sit_id})
    except Exception as e:
        log.error("_update_situation_record(%s): %s", sit_id, e)


def _sync_situation_tags_for_item(item_id: str) -> None:
    """
    Recompute ``project_tag`` on every situation that contains ``item_id``.

    Collects the union of all member project tags (multi-tag aware).

    :param item_id: Stable ID of the analysis whose tag just changed.
    :type item_id: str
    """
    with db.lock:
        affected = db.get_situations_containing_item(item_id)
    for sit in affected:
        sit_id   = sit.get("situation_id")
        mem_ids  = sit.get("item_ids", [])
        with db.lock:
            members = [db.get_item(iid) for iid in mem_ids]
        members   = [m for m in members if m]
        all_tags  = set()
        for m in members:
            all_tags.update(db.parse_project_tags(m.get("project_tag")))
        new_tag = db.serialize_project_tags(sorted(all_tags)) if all_tags else None
        if new_tag != sit.get("project_tag"):
            with db.lock:
                db.update_situation(sit_id, {"project_tag": new_tag})


def _sync_situation_tags_all() -> None:
    """
    Recompute ``project_tag`` on every situation.

    Called after a bulk project-tag change (e.g. project deletion in
    ``save_settings``).  Multi-tag aware — collects union of member tags.
    """
    with db.lock:
        all_sits = db.get_all_situations(include_dismissed=True)
    for sit in all_sits:
        sit_id   = sit.get("situation_id")
        mem_ids  = sit.get("item_ids", [])
        with db.lock:
            members = [db.get_item(iid) for iid in mem_ids]
        members   = [m for m in members if m]
        all_tags  = set()
        for m in members:
            all_tags.update(db.parse_project_tags(m.get("project_tag")))
        new_tag = db.serialize_project_tags(sorted(all_tags)) if all_tags else None
        if new_tag != sit.get("project_tag"):
            with db.lock:
                db.update_situation(sit_id, {"project_tag": new_tag})


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

    All writes are serialised through ``db.lock``.  Exceptions are caught and
    logged so a single failing item does not block the ingest queue.

    :param item_id: Stable ID of the analysis item to process.
    :type item_id: str
    """
    try:
        from embedder import get_item_vector

        with db.lock:
            record       = db.get_item(item_id)
            all_analyses = db.get_all_items()
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
        with db.lock:
            sit_ids = set()
            for cid in candidates:
                r = db.get_item(cid)
                if r and r.get("situation_id"):
                    sit_ids.add(r["situation_id"])

        if sit_ids:
            # Merge into the highest-scoring existing situation
            target_sit_id = sit_ids.pop()
            with db.lock:
                sit = db.get_situation(target_sit_id)
            if sit:
                updated_ids = list(dict.fromkeys(sit.get("item_ids", []) + [item_id]))
                # Merge any additional sit_ids
                for extra_sid in sit_ids:
                    with db.lock:
                        extra = db.get_situation(extra_sid)
                    if extra:
                        updated_ids = list(dict.fromkeys(updated_ids + extra.get("item_ids", [])))
                        with db.lock:
                            db.delete_situation(extra_sid)

                _update_situation_record(target_sit_id, updated_ids)
                with db.lock:
                    db.update_item(item_id, {"situation_id": target_sit_id})
                return

        # No existing situation — check minimum cluster requirements
        all_ids = [item_id] + candidates
        with db.lock:
            cluster_records = [db.get_item(iid) for iid in all_ids]
        cluster_records = [r for r in cluster_records if r]

        if len(cluster_records) < 2:
            return

        if _scan_state.get("cancelled"):
            return

        # Create new situation
        with db.lock:
            cluster_intel = db.get_intel_for_items(list(set(all_ids)))
        synthesis   = _correlator.synthesize_situation(cluster_records, config.USER_NAME or "the user", intel_items=cluster_intel)
        score       = _correlator.score_situation(all_ids, cluster_records)
        max_pri     = max(cluster_records, key=lambda r: _pri_rank(r.get("priority", "low")))
        all_refs    = list(set(r for rec in cluster_records
                               for raw in [rec.get("references")]
                               for r in (json.loads(raw) if isinstance(raw, str) and raw else (raw or []))))
        all_proj = set()
        for r in cluster_records:
            all_proj.update(db.parse_project_tags(r.get("project_tag")))
        proj_tag_val = db.serialize_project_tags(sorted(all_proj)) if all_proj else None
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
            "project_tag":      proj_tag_val,
            "score":            score,
            "priority":         max_pri.get("priority", "medium"),
            "open_actions":     synthesis.get("open_actions", []),
            "references":       all_refs,
            "key_context":        synthesis.get("key_context"),
            "last_updated":       last_ts,
            "created_at":         now_iso(),
            "score_updated_at":   now_iso(),
            "dismissed":          False,
            "lifecycle_status":   "new",
            "notes":              "",
        }
        with db.lock:
            db.insert_situation(sit_doc)
            for iid in all_ids:
                db.update_item(iid, {"situation_id": sit_id})

        log.info("formed situation %s from %d items", sit_id[:8], len(all_ids))

    except Exception as e:
        log.error("_maybe_form_situation(%s): %s", item_id, e)


def _spawn_situation_task(item_id: str) -> None:
    """
    Spawn ``_maybe_form_situation`` in a daemon thread and track it in ``scan_state``.

    Increments ``_scan_state["situations_pending"]`` before starting the thread
    and decrements it in a ``finally`` block so the counter stays accurate even
    when situation formation raises an exception.

    :param item_id: Stable ID of the analysis item to process.
    :type item_id: str
    """
    with db.lock:
        _scan_state["situations_pending"] += 1

    def _run() -> None:
        try:
            _maybe_form_situation(item_id)
        finally:
            with db.lock:
                _scan_state["situations_pending"] = max(0, _scan_state["situations_pending"] - 1)

    threading.Thread(target=_run, daemon=True).start()


# ── Split / merge ────────────────────────────────────────────────────────────

def _rescore_lightweight(sit_id: str, item_ids: list) -> None:
    """
    Recompute ``score``, ``priority``, ``sources``, ``last_updated``, and
    ``project_tag`` for a situation without invoking the LLM.

    Used after split/merge so the caller sees a consistent record without
    paying for synthesis.  The user can trigger full rescoring via
    ``POST /situations/{id}/rescore``.
    """
    with db.lock:
        records = [db.get_item(iid) for iid in item_ids]
    records = [r for r in records if r]
    if not records:
        return
    score    = _correlator.score_situation(item_ids, records)
    max_pri  = max(records, key=lambda r: _pri_rank(r.get("priority", "low")))
    sources  = list(set(r.get("source", "") for r in records))
    last_ts  = max((r.get("timestamp", "") for r in records), default=now_iso())
    all_proj = set()
    for r in records:
        all_proj.update(db.parse_project_tags(r.get("project_tag")))
    proj_tag_val = db.serialize_project_tags(sorted(all_proj)) if all_proj else None
    with db.lock:
        db.update_situation(sit_id, {
            "item_ids":         item_ids,
            "sources":          sources,
            "score":            score,
            "priority":         max_pri.get("priority", "medium"),
            "last_updated":     last_ts,
            "project_tag":      proj_tag_val,
            "score_updated_at": now_iso(),
        })


def split_situation(
    sit_id: str,
    item_ids_to_split: list[str],
    new_title: str | None = None,
) -> str:
    """
    Move a subset of items out of ``sit_id`` into a brand-new situation.

    Both situations are rescored lightweight (no LLM).  The caller gets back
    the new situation's UUID so the UI can focus it.

    :raises ValueError: if ``item_ids_to_split`` is empty, contains ids not in
                        the source situation, or would leave either situation
                        empty.
    """
    with db.lock:
        src = db.get_situation(sit_id)
    if not src:
        raise ValueError("situation not found")
    src_ids = list(src.get("item_ids", []))
    if not item_ids_to_split:
        raise ValueError("item_ids_to_split is empty")
    unknown = [i for i in item_ids_to_split if i not in src_ids]
    if unknown:
        raise ValueError(f"items not in source situation: {unknown}")
    remaining = [i for i in src_ids if i not in item_ids_to_split]
    if not remaining:
        raise ValueError("split would empty the source situation — dismiss or keep at least one item")
    if len(item_ids_to_split) == len(src_ids):
        raise ValueError("cannot split all items — would empty source")

    new_sit_id = str(_uuid.uuid4())
    title = new_title or f"{src.get('title','Split')} (split)"
    sit_doc = {
        "situation_id":     new_sit_id,
        "title":            title,
        "summary":          src.get("summary") or "",
        "status":           src.get("status") or "in_progress",
        "item_ids":         item_ids_to_split,
        "sources":          [],
        "project_tag":      None,
        "score":            0.0,
        "priority":         src.get("priority") or "medium",
        "open_actions":     [],
        "references":       [],
        "key_context":      None,
        "last_updated":     now_iso(),
        "created_at":       now_iso(),
        "score_updated_at": now_iso(),
        "dismissed":        False,
        "lifecycle_status": "new",
        "notes":            f"Split from {sit_id}",
    }
    with db.lock:
        db.insert_situation(sit_doc)
        for iid in item_ids_to_split:
            db.update_item(iid, {"situation_id": new_sit_id})
        db.insert_situation_event(new_sit_id, None, "new", f"split from {sit_id}")
        db.insert_situation_event(sit_id, None, src.get("lifecycle_status") or "new",
                                  f"split {len(item_ids_to_split)} item(s) to {new_sit_id}")
    _rescore_lightweight(sit_id, remaining)
    _rescore_lightweight(new_sit_id, item_ids_to_split)
    return new_sit_id


def merge_situations(target_id: str, source_id: str) -> None:
    """
    Merge ``source_id`` into ``target_id``.

    All source items are relinked to the target, the target is rescored
    lightweight, and the source is dismissed with reason ``merged_into:<id>``
    so history is preserved.

    :raises ValueError: if either situation is missing or target == source.
    """
    if target_id == source_id:
        raise ValueError("target and source must differ")
    with db.lock:
        target = db.get_situation(target_id)
        source = db.get_situation(source_id)
    if not target or not source:
        raise ValueError("target or source situation not found")

    combined_ids = list(target.get("item_ids", []))
    for iid in source.get("item_ids", []):
        if iid not in combined_ids:
            combined_ids.append(iid)

    with db.lock:
        for iid in source.get("item_ids", []):
            db.update_item(iid, {"situation_id": target_id})
        prev = source.get("lifecycle_status", "new")
        db.update_situation(source_id, {
            "dismissed":        1,
            "dismiss_reason":   f"merged_into:{target_id}",
            "lifecycle_status": "dismissed",
            "item_ids":         [],
        })
        db.insert_situation_event(source_id, prev, "dismissed",
                                  f"merged into {target_id}")
        db.insert_situation_event(target_id, None,
                                  target.get("lifecycle_status") or "new",
                                  f"merged in {source_id} "
                                  f"({len(source.get('item_ids', []))} items)")
    _rescore_lightweight(target_id, combined_ids)


# ── Stale-decay helpers ──────────────────────────────────────────────────────

# A situation in "waiting" for this many days without activity is flagged stale.
STALE_WAITING_DAYS       = 7
# A situation in "investigating" for this many days without activity is flagged.
STALE_INVESTIGATING_DAYS = 14


def _days_since(ts: str | None) -> float | None:
    """Return days between ``ts`` (ISO string) and now, or None if unparseable."""
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except Exception:
        return None


def _compute_stale_flag(sit: dict) -> str | None:
    """
    Return a stale-decay label for ``sit`` or None if it's fresh.

    Labels:
      * ``"stale_waiting"``       — in ``waiting`` ≥ STALE_WAITING_DAYS with no
        event newer than that threshold.
      * ``"stale_investigating"`` — in ``investigating`` ≥
        STALE_INVESTIGATING_DAYS with no event newer than that threshold.
    """
    lifecycle = sit.get("lifecycle_status", "")
    if lifecycle not in ("waiting", "investigating"):
        return None
    threshold = STALE_WAITING_DAYS if lifecycle == "waiting" else STALE_INVESTIGATING_DAYS
    try:
        events = db.get_situation_events(sit.get("situation_id"))
    except Exception:
        events = []
    # Find the most recent event *timestamp*; fall back to last_updated.
    latest_event_ts = max((e.get("timestamp") or "" for e in events), default="")
    ref_ts = latest_event_ts or sit.get("last_updated")
    age_days = _days_since(ref_ts)
    if age_days is not None and age_days >= threshold:
        return f"stale_{lifecycle}"
    return None


# ── Response builder ───────────────────────────────────────────────────────────

def _situation_response(sit: dict) -> dict:
    """
    Build the API response dict for a situation.

    Fetches a lightweight summary (``item_id``, ``source``, ``title``,
    ``priority``, ``timestamp``) for each contributing analysis and includes
    it in the response as the ``items`` list.

    :param sit: Raw situation document dict from db.py.
    :type sit: dict
    :return: API-ready dict with all situation fields plus lightweight ``items``.
    :rtype: dict
    """
    item_ids = sit.get("item_ids", [])
    with db.lock:
        items_raw = [db.get_item(iid) for iid in item_ids]
        all_todos = []
        for iid in item_ids:
            all_todos.extend(db.get_todos_for_item(iid))
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
    done_keys = {
        (t["item_id"], t["description"])
        for t in all_todos if t.get("done")
    }
    from datetime import datetime, timezone
    follow_up_date   = sit.get("follow_up_date")
    follow_up_overdue = False
    if follow_up_date:
        try:
            fud = datetime.fromisoformat(follow_up_date.rstrip("Z"))
            follow_up_overdue = fud < datetime.now(timezone.utc).replace(tzinfo=None)
        except Exception:
            pass

    return {
        "situation_id":    sit.get("situation_id"),
        "title":           sit.get("title"),
        "summary":         sit.get("summary"),
        "status":          sit.get("status"),
        "lifecycle_status": sit.get("lifecycle_status", "new"),
        "score":           sit.get("score", 0.0),
        "priority":        sit.get("priority"),
        "sources":         sit.get("sources", []),
        "project_tag":     sit.get("project_tag"),
        "open_actions":    [
            {**a, "done": (a.get("source_item_id"), a.get("description")) in done_keys}
            for a in sit.get("open_actions", [])
        ],
        "references":      sit.get("references", []),
        "key_context":     sit.get("key_context"),
        "last_updated":    sit.get("last_updated"),
        "created_at":      sit.get("created_at"),
        "dismissed":       sit.get("dismissed", False),
        "follow_up_date":  follow_up_date,
        "follow_up_overdue": follow_up_overdue,
        "notes":           sit.get("notes", ""),
        "item_count":      len(items),
        "items":           items,
        "stale_flag":      _compute_stale_flag(sit),
    }
