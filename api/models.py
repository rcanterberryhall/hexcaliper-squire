"""
models.py — Core data models for the Squire analysis pipeline.

Defines the two-stage data flow:

1. ``RawItem`` — a normalised, source-agnostic representation of an inbound
   item (email, Slack message, GitHub notification, Jira issue) before AI
   processing.
2. ``Analysis`` — the structured result produced by the LLM for a single
   ``RawItem``, including priority, category, action items, summary, and
   context-aware enrichment fields (hierarchy, passdown flag, project tag,
   extracted goals, key dates, and a body preview for keyword learning).
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawItem:
    """
    A normalised, source-agnostic item ready for AI analysis.

    Connectors and sidecar scripts produce ``RawItem`` objects that are
    passed to ``agent.analyze_batch`` regardless of their origin.

    :ivar source: Originating system identifier, e.g. ``"outlook"``, ``"slack"``.
    :ivar item_id: Stable unique ID for this item used for deduplication.
    :ivar title: Short human-readable title or subject line.
    :ivar body: Full text content, truncated to 3 000 characters by convention.
    :ivar url: Deep link back to the source item, or empty string if unavailable.
    :ivar author: Display name and/or email of the sender or creator.
    :ivar timestamp: ISO 8601 timestamp of when the item was created or received.
    :ivar metadata: Arbitrary source-specific key/value pairs (channel, status, etc.).
    """
    source:    str
    item_id:   str
    title:     str
    body:      str
    url:       str
    author:    str
    timestamp: str
    metadata:  dict = field(default_factory=dict)


@dataclass
class ActionItem:
    """
    A single concrete action extracted from an analysed item.

    :ivar description: Human-readable description of the required action.
    :ivar deadline: ISO 8601 date string if a due date was identified, else ``None``.
    :ivar owner: Who is responsible — ``"me"`` or a named person.
    """
    description: str
    deadline:    Optional[str]
    owner:       str


@dataclass
class Analysis:
    """
    The structured result of running a ``RawItem`` through the LLM pipeline.

    :ivar item_id: ID of the originating ``RawItem``.
    :ivar source: Originating system identifier.
    :ivar title: Title carried forward from the ``RawItem``.
    :ivar author: Author carried forward from the ``RawItem``.
    :ivar timestamp: Timestamp carried forward from the ``RawItem``.
    :ivar url: Deep link carried forward from the ``RawItem``.
    :ivar has_action: ``True`` if at least one action item was identified.
    :ivar priority: Urgency level — one of ``"high"``, ``"medium"``, ``"low"``.
    :ivar category: Item classification — one of ``"reply_needed"``, ``"task"``,
                    ``"deadline"``, ``"review"``, ``"approval"``, ``"fyi"``, ``"noise"``.
    :ivar action_items: List of concrete actions extracted by the LLM.
    :ivar summary: One-sentence summary of the item and required action.
    :ivar urgency_reason: Brief explanation of the assigned priority, or ``None``.
    :ivar hierarchy: Relevance tier — ``"user"`` (directly addressed to the
                     configured user), ``"project"`` (related to an active
                     project), ``"topic"`` (matches a watch topic), or
                     ``"general"`` (everything else). Defaults to ``"general"``.
    :ivar is_passdown: ``True`` when the item is a shift handoff/passdown note.
                       Set deterministically by ``_detect_passdown`` and can
                       also be set by the LLM. Defaults to ``False``.
    :ivar project_tag: Name of the configured project this item belongs to, or
                       ``None`` if untagged. Can be set by the LLM or manually
                       via ``POST /analyses/{item_id}/tag``.
    :ivar goals: Project goals or objectives extracted from the content by the
                 LLM (not individual tasks). Defaults to an empty list.
    :ivar key_dates: Deadlines, release dates, or other time references
                     extracted from the content, each a dict with ``"date"``
                     and ``"description"`` keys. Defaults to an empty list.
    :ivar body_preview: First 500 characters of the item body, stored for
                        post-hoc keyword learning without re-fetching content.
                        Defaults to an empty string.
    :ivar to_field: Raw ``To`` header value carried forward from item metadata.
                    Used for post-hoc sender/group learning. Defaults to ``""``.
    :ivar cc_field: Raw ``CC`` header value carried forward from item metadata.
                    Used for post-hoc sender/group learning. Defaults to ``""``.
    """
    item_id:        str
    source:         str
    title:          str
    author:         str
    timestamp:      str
    url:            str
    has_action:     bool
    priority:       str
    category:       str
    action_items:   list[ActionItem]
    summary:        str
    urgency_reason: Optional[str]
    # Context-aware enrichment fields
    hierarchy:      str            = "general"   # "user" | "project" | "topic" | "general"
    is_passdown:    bool           = False
    project_tag:    Optional[str]  = None
    goals:          list[str]      = field(default_factory=list)
    key_dates:      list[dict]     = field(default_factory=list)
    body_preview:      str            = ""
    to_field:          str            = ""
    cc_field:          str            = ""
    is_replied:        bool           = False
    replied_at:        Optional[str]  = None
    information_items: list[dict]     = field(default_factory=list)


@dataclass
class Situation:
    """
    A cross-source grouping of related Analysis items.

    :ivar situation_id: Stable UUID for this situation.
    :ivar title: LLM-generated short title.
    :ivar summary: LLM-generated cross-source narrative (2-3 sentences).
    :ivar status: Operational status — "blocked", "in_progress", "waiting",
                  "needs_decision", or "informational".
    :ivar item_ids: Ordered list of contributing analysis item_ids,
                    most recently updated first.
    :ivar sources: Unique source types present in this situation.
    :ivar project_tag: Shared project tag if all items agree, else None.
    :ivar score: Composite urgency score from score_situation().
    :ivar priority: Derived from highest-priority contributing item.
    :ivar open_actions: Deduplicated action items across all contributing items.
    :ivar references: Union of all extracted reference strings from members.
    :ivar key_context: LLM-extracted essential background sentence, or None.
    :ivar last_updated: ISO timestamp of most recently updated contributing item.
    :ivar created_at: ISO timestamp when this situation was first formed.
    :ivar score_updated_at: ISO timestamp of last score recomputation.
    """
    situation_id:     str
    title:            str
    summary:          str
    status:           str
    item_ids:         list
    sources:          list
    project_tag:      Optional[str]
    score:            float
    priority:         str
    open_actions:     list
    references:       list
    key_context:      Optional[str]
    last_updated:     str
    created_at:       str
    score_updated_at: str
