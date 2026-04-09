"""
graph.py — Knowledge graph layer for Squire.

Builds and queries a lightweight directed graph of items, people, projects,
and conversations stored in the nodes/edges tables via db.py.

Two primary entry points
────────────────────────
  index_item(analysis)            — ingest a saved Analysis into the graph,
                                    creating/updating nodes and weighted edges
  get_context(item, max_n=5)      — retrieve the most relevant prior items
                                    to inject as context into the analysis prompt
  format_context(context_items)   — render context items as a prompt string

Node types
──────────
  item          — one per Analysis; label = title[:80]
  person        — one per unique author e-mail; label = display name
  project       — one per configured project tag; label = project name
  conversation  — one per conversation_id (email thread); label = subject

Edge types and base weights
────────────────────────────
  in_conversation  item → conversation   1.00  (same email thread)
  in_situation     item → situation      0.80  (grouped by LLM)
  tagged_to        item → project        0.55  (same project)
  authored_by      item → person         0.40  (same sender/author)

Scoring
───────
context_score(edge_weight, item_timestamp) =
    edge_weight × recency_decay(item_timestamp)

recency_decay(t) = exp(−age_days / HALF_LIFE_DAYS)
  HALF_LIFE_DAYS = 14  →  same-day item ≈ 1.0, 14-day-old item ≈ 0.5
"""

import math
from datetime import datetime, timezone
from typing import Optional

import db
from agent import extract_emails

# ── Scoring constants ──────────────────────────────────────────────────────────

HALF_LIFE_DAYS = 14.0   # recency half-life in days

EDGE_WEIGHTS = {
    "in_conversation": 1.00,
    "in_situation":    0.80,
    "tagged_to":       0.55,
    "authored_by":     0.40,
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> Optional[datetime]:
    """Parse an ISO timestamp, returning None on failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _recency_decay(timestamp: str) -> float:
    """
    Return a recency weight in (0, 1] based on item age.

    Uses exponential decay with HALF_LIFE_DAYS so that an item from the same
    day scores ≈1.0 and an item 14 days old scores ≈0.5.
    """
    dt = _parse_ts(timestamp)
    if dt is None:
        return 0.1
    age_days = (_now() - dt).total_seconds() / 86400
    return math.exp(-age_days * math.log(2) / HALF_LIFE_DAYS)


def _person_id(email: str) -> str:
    return f"person:{email.lower()}"


def _project_id(project_tag: str) -> str:
    return f"project:{project_tag}"


def _conversation_id_node(conv_id: str) -> str:
    return f"conv:{conv_id}"


def _situation_id_node(sit_id: str) -> str:
    return f"sit:{sit_id}"


def _item_id_node(item_id: str) -> str:
    return f"item:{item_id}"


# ── Indexing ───────────────────────────────────────────────────────────────────

def index_item(analysis) -> None:
    """
    Ingest a saved Analysis object into the knowledge graph.

    Creates or updates nodes for the item, its author, project, and
    conversation, then upserts directed edges connecting them.  Safe to
    call multiple times for the same item (all operations are idempotent).

    :param analysis: A saved ``Analysis`` dataclass instance.
    """
    item_node = _item_id_node(analysis.item_id)

    # Item node
    db.upsert_node(
        node_id   = item_node,
        node_type = "item",
        label     = analysis.title[:80],
        properties = {
            "source":    analysis.source,
            "timestamp": analysis.timestamp,
            "priority":  analysis.priority,
            "category":  analysis.category,
        },
    )

    # Author → item edge
    emails = extract_emails(analysis.author)
    primary_email = emails[0] if emails else analysis.author.strip().lower()
    if primary_email:
        person_node = _person_id(primary_email)
        db.upsert_node(
            node_id   = person_node,
            node_type = "person",
            label     = analysis.author[:80],
            properties = {"email": primary_email},
        )
        db.upsert_edge(
            src_id    = item_node,
            dst_id    = person_node,
            edge_type = "authored_by",
            weight    = EDGE_WEIGHTS["authored_by"],
        )

    # Project edges (one per tag for multi-tagged items)
    for ptag in db.parse_project_tags(analysis.project_tag):
        proj_node = _project_id(ptag)
        db.upsert_node(
            node_id   = proj_node,
            node_type = "project",
            label     = ptag,
        )
        db.upsert_edge(
            src_id    = item_node,
            dst_id    = proj_node,
            edge_type = "tagged_to",
            weight    = EDGE_WEIGHTS["tagged_to"],
        )

    # Conversation edge
    if analysis.conversation_id:
        conv_node = _conversation_id_node(analysis.conversation_id)
        label = analysis.conversation_topic or analysis.title[:80]
        db.upsert_node(
            node_id   = conv_node,
            node_type = "conversation",
            label     = label,
            properties = {"topic": analysis.conversation_topic or ""},
        )
        db.upsert_edge(
            src_id    = item_node,
            dst_id    = conv_node,
            edge_type = "in_conversation",
            weight    = EDGE_WEIGHTS["in_conversation"],
        )

    # Situation edge (if already grouped)
    if getattr(analysis, "situation_id", None):
        sit_node = _situation_id_node(analysis.situation_id)
        db.upsert_edge(
            src_id    = item_node,
            dst_id    = sit_node,
            edge_type = "in_situation",
            weight    = EDGE_WEIGHTS["in_situation"],
        )


def index_item_situation(item_id: str, situation_id: str) -> None:
    """
    Add a situation edge for an item that has been grouped after initial indexing.

    :param item_id: The item's ID.
    :param situation_id: The situation's ID.
    """
    db.upsert_edge(
        src_id    = _item_id_node(item_id),
        dst_id    = _situation_id_node(situation_id),
        edge_type = "in_situation",
        weight    = EDGE_WEIGHTS["in_situation"],
    )


# ── Context query ──────────────────────────────────────────────────────────────

def _candidates_via_edge_type(
    hub_node: str,
    edge_type: str,
    base_weight: float,
    exclude_item_id: str,
) -> list[dict]:
    """
    Find item nodes connected to hub_node via a given edge type, returning
    scored candidates.  Edges are traversed in both directions.
    """
    candidates = []

    # Items pointing TO the hub (item → hub)
    sibling_edges = db.get_edges_to(hub_node, edge_type=edge_type)
    for e in sibling_edges:
        other_item_node = e["src_id"]
        if not other_item_node.startswith("item:"):
            continue
        other_item_id = other_item_node[len("item:"):]
        if other_item_id == exclude_item_id:
            continue
        candidates.append({
            "item_id":    other_item_id,
            "edge_type":  edge_type,
            "base_weight": base_weight,
        })

    return candidates


def get_context(item, max_n: int = 5) -> list[dict]:
    """
    Retrieve the most relevant prior items from the graph for a given item.

    Looks up the item's conversation, author, and project nodes, collects
    all connected items, scores each by ``base_weight × recency_decay``, and
    returns the top ``max_n`` as full item dicts from the items table.

    The result is empty if the item has no matching nodes in the graph yet
    (e.g. first scan run).

    :param item: A ``RawItem`` or an ``Analysis`` — any object with
                 ``item_id`` and ``metadata`` attributes, or an ``item_id``
                 str plus ``source``, ``author``, ``conversation_id``,
                 ``conversation_topic``, and ``project_tag`` attributes.
    :param max_n: Maximum number of context items to return.
    :return: List of scored item dicts sorted by descending context score.
             Each dict is a plain items-table row with an added
             ``"context_score"`` and ``"context_edge"`` key.
    """
    item_id = item.item_id if hasattr(item, "item_id") else str(item)

    # Extract connection keys from either RawItem (uses .metadata) or Analysis
    if hasattr(item, "metadata"):
        meta         = item.metadata
        conversation = meta.get("conversation_id", "")
        author_raw   = item.author
        project_tag  = meta.get("project_tag", "")
    else:
        conversation = getattr(item, "conversation_id", "") or ""
        author_raw   = getattr(item, "author", "")
        project_tag  = getattr(item, "project_tag", "") or ""

    scored: dict[str, dict] = {}  # item_id → best score so far

    def _add_candidates(hub_node: str, edge_type: str) -> None:
        base_weight = EDGE_WEIGHTS.get(edge_type, 0.3)
        for c in _candidates_via_edge_type(hub_node, edge_type, base_weight, item_id):
            row = db.get_item(c["item_id"])
            if not row:
                continue
            decay = _recency_decay(row.get("timestamp", ""))
            score = base_weight * decay
            if c["item_id"] not in scored or scored[c["item_id"]]["context_score"] < score:
                scored[c["item_id"]] = {
                    **row,
                    "context_score": round(score, 4),
                    "context_edge":  edge_type,
                }

    # 1. Same email thread (highest signal)
    if conversation:
        _add_candidates(_conversation_id_node(conversation), "in_conversation")

    # 2. Same author
    emails = extract_emails(author_raw)
    primary_email = emails[0] if emails else author_raw.strip().lower()
    if primary_email:
        _add_candidates(_person_id(primary_email), "authored_by")

    # 3. Same project(s)
    for ptag in db.parse_project_tags(project_tag):
        _add_candidates(_project_id(ptag), "tagged_to")

    # Sort by context_score descending and return top N
    ranked = sorted(scored.values(), key=lambda x: x["context_score"], reverse=True)
    return ranked[:max_n]


# ── Prompt formatting ──────────────────────────────────────────────────────────

def format_context(context_items: list[dict]) -> str:
    """
    Render a list of context items as a human-readable prompt section.

    Groups items by ``context_edge`` so the LLM can see which relationship
    each item arrived through.

    :param context_items: Output of ``get_context()``.
    :return: Multi-line string ready to embed in the analysis prompt, or
             empty string if ``context_items`` is empty.
    """
    if not context_items:
        return ""

    _EDGE_LABELS = {
        "in_conversation": "same email thread",
        "in_situation":    "related situation",
        "tagged_to":       "same project",
        "authored_by":     "same author",
    }

    lines = ["Prior context from related items:"]
    for c in context_items:
        edge_label = _EDGE_LABELS.get(c.get("context_edge", ""), "related")
        source     = c.get("source", "")
        title      = c.get("title", "")[:70]
        summary    = c.get("summary", "")[:120]
        priority   = c.get("priority", "")
        ts         = (c.get("timestamp") or "")[:10]
        score      = c.get("context_score", 0.0)

        lines.append(
            f"  [{edge_label}] [{source}] {title} "
            f"(priority: {priority}, date: {ts}, relevance: {score:.2f})"
        )
        if summary:
            lines.append(f"    Summary: {summary}")

    return "\n".join(lines)
