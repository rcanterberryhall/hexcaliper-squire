"""
attention.py — Adaptive attention model for Parsival.

Tracks implicit user behavioral signals to learn which items deserve
attention.  Maintains two centroid embeddings in the model_state table:

  attended_centroid  — items opened, tagged, investigated, deep-analysed
  ignored_centroid   — items never touched within 48h, or explicitly noised

Attention score for a new item::

    raw = cosine(embedding, attended_centroid)
          - 0.5 * cosine(embedding, ignored_centroid)

Normalized to 0–1.  Returns 0.5 (neutral) during cold start (< 50 actions)
or when embeddings are unavailable.

Cold start message
------------------
``is_cold_start()`` returns True when fewer than COLD_START_THRESHOLD actions
have been recorded.

Recency decay
-------------
Actions older than DECAY_30_DAYS days contribute at 0.5 weight;
older than DECAY_60_DAYS days at 0.25 weight.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone, timedelta

import db

try:
    import numpy as np
    _NP = True
except ImportError:
    _NP = False

try:
    import embedder as _embedder
    _EMB = True
except ImportError:
    _EMB = False

COLD_START_THRESHOLD = 50
DECAY_30_DAYS        = 30
DECAY_60_DAYS        = 60

_ATTENDED_ACTIONS = frozenset({"opened", "tagged", "investigated_situation",
                                "deep_analysis", "todo_created"})
_IGNORED_ACTIONS  = frozenset({"noised", "dismissed_situation"})

_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decay_weight(ts_iso: str, now: datetime) -> float:
    """Return 1.0, 0.5, or 0.25 depending on age of *ts_iso*."""
    try:
        ts  = datetime.fromisoformat(ts_iso)
        age = (now - ts).days
    except Exception:
        return 1.0
    if age > DECAY_60_DAYS:
        return 0.25
    if age > DECAY_30_DAYS:
        return 0.5
    return 1.0


def _cosine(a: list, b: list) -> float:
    if not _NP or not a or not b:
        return 0.0
    va = np.array(a, dtype=float)
    vb = np.array(b, dtype=float)
    na = np.linalg.norm(va)
    nb = np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def _weighted_centroid(vectors: list[list], weights: list[float]) -> list | None:
    """Compute a weighted average centroid, L2-normalized."""
    if not _NP or not vectors:
        return None
    total_w = sum(weights)
    if total_w == 0:
        return None
    arr = np.zeros(len(vectors[0]), dtype=float)
    for v, w in zip(vectors, weights):
        arr += np.array(v, dtype=float) * w
    arr /= total_w
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr = arr / norm
    return arr.tolist()


# ── Public API ─────────────────────────────────────────────────────────────────

def is_cold_start() -> bool:
    """Return True when fewer than COLD_START_THRESHOLD actions are recorded."""
    with db.lock:
        return db.count_user_actions() < COLD_START_THRESHOLD


def record_action(item_id: str, action_type: str) -> None:
    """Log a user interaction with an item."""
    with db.lock:
        db.record_user_action(item_id, action_type)
    # Trigger centroid update in background
    t = threading.Thread(target=_update_centroids, daemon=True)
    t.start()


def _update_centroids() -> None:
    """Recompute attended/ignored centroids from all recorded actions."""
    if not _EMB or not _NP:
        return
    with _lock:
        now = _now()
        with db.lock:
            actions = db.get_user_actions()

        attended_vecs:  list[list]  = []
        attended_ws:    list[float] = []
        ignored_vecs:   list[list]  = []
        ignored_ws:     list[float] = []

        for action in actions:
            atype  = action.get("action_type", "")
            item_id = action.get("item_id", "")
            w = _decay_weight(action.get("timestamp", ""), now)

            vec = _embedder.get_item_vector(item_id)
            if not vec:
                continue

            if atype in _ATTENDED_ACTIONS:
                attended_vecs.append(vec)
                attended_ws.append(w)
            elif atype in _IGNORED_ACTIONS:
                ignored_vecs.append(vec)
                ignored_ws.append(w)

        attended_c = _weighted_centroid(attended_vecs, attended_ws)
        ignored_c  = _weighted_centroid(ignored_vecs,  ignored_ws)

        state: dict = {}
        if attended_c:
            state["attended_centroid"]       = attended_c
            state["attended_count"]          = len(attended_vecs)
        if ignored_c:
            state["ignored_centroid"]        = ignored_c
            state["ignored_count"]           = len(ignored_vecs)
        state["updated_at"] = now.isoformat()

        with db.lock:
            db.set_model_state("attention", state)


def compute_score(item_embedding: list) -> float:
    """
    Return attention score in [0, 1].  Returns 0.5 on cold start or unavailability.
    """
    if not item_embedding or not _NP:
        return 0.5

    with db.lock:
        if db.count_user_actions() < COLD_START_THRESHOLD:
            return 0.5
        state = db.get_model_state("attention") or {}

    attended_c = state.get("attended_centroid")
    ignored_c  = state.get("ignored_centroid")

    if not attended_c:
        return 0.5

    raw = _cosine(item_embedding, attended_c)
    if ignored_c:
        raw -= 0.5 * _cosine(item_embedding, ignored_c)

    # Normalize raw [-1.5, 1] to [0, 1]
    normalized = (raw + 1.5) / 2.5
    return max(0.0, min(1.0, normalized))


def get_why(item_embedding: list) -> str:
    """Return a human-readable explanation for the attention score."""
    if not item_embedding or not _NP or is_cold_start():
        return ""
    with db.lock:
        state = db.get_model_state("attention") or {}
    attended_c = state.get("attended_centroid")
    ignored_c  = state.get("ignored_centroid")
    if not attended_c:
        return ""
    sim_att = _cosine(item_embedding, attended_c)
    sim_ign = _cosine(item_embedding, ignored_c) if ignored_c else 0.0
    if sim_att > 0.7:
        return "Similar to items you have recently acted on."
    if sim_att > 0.5:
        return "Partially similar to items you have investigated."
    if sim_ign > 0.6:
        return "Similar to items you typically ignore."
    return "Not strongly similar to any recent pattern."


def get_summary() -> dict:
    """
    Return a summary dict for the merLLM 'My Day' panel.

    Includes high-attention item count, cold-start flag, and centroid freshness.
    """
    with db.lock:
        action_count = db.count_user_actions()
        state        = db.get_model_state("attention") or {}
    return {
        "action_count":    action_count,
        "cold_start":      action_count < COLD_START_THRESHOLD,
        "cold_start_msg":  (
            "Learning your attention patterns — prioritization will improve "
            "as you use the tool."
            if action_count < COLD_START_THRESHOLD else ""
        ),
        "attended_count":  state.get("attended_count", 0),
        "ignored_count":   state.get("ignored_count", 0),
        "last_updated":    state.get("updated_at"),
    }
