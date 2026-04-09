"""
models.py — Core data models for the Squire analysis pipeline.

Defines the two-stage data flow:

1. ``RawItem`` — a normalised, source-agnostic representation of an inbound
   item (email, Slack message, GitHub notification, Jira issue) before AI
   processing.
2. ``Analysis`` — the structured result produced by the LLM for a single
   ``RawItem``, including priority, category, action items, summary, and
   context-aware enrichment fields.

Category schema (4 categories + task_type):
  task      — actionable work for the user; task_type refines the sub-type:
                "reply"  — someone is waiting on a response
                "review" — user must review, proofread, approve, or sign off
                null     — general assigned work
  approval  — an approval event has occurred (PO approved, plan signed off,
               timesheets approved) — either the user approved or it affects them
  fyi       — informational only; no action required
  noise     — automated or irrelevant; no action required
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawItem:
    """
    A normalised, source-agnostic item ready for AI analysis.

    :ivar source: Originating system identifier, e.g. ``"outlook"``, ``"slack"``.
    :ivar item_id: Stable unique ID for this item used for deduplication.
    :ivar title: Short human-readable title or subject line.
    :ivar body: Full text content, truncated to 3 000 characters by convention.
    :ivar url: Deep link back to the source item, or empty string if unavailable.
    :ivar author: Display name and/or email of the sender or creator.
    :ivar timestamp: ISO 8601 timestamp of when the item was created or received.
    :ivar metadata: Arbitrary source-specific key/value pairs (channel, status,
                    conversation_id, direction, etc.).
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
    :ivar category: Item classification — one of ``"task"``, ``"approval"``,
                    ``"fyi"``, ``"noise"``.
    :ivar task_type: Sub-type for task items — ``"review"``, ``"reply"``, or
                     ``None`` for general tasks.
    :ivar action_items: List of concrete actions extracted by the LLM.
    :ivar summary: One-sentence summary of the item and required action.
    :ivar urgency_reason: Brief explanation of the assigned priority, or ``None``.
    :ivar hierarchy: Relevance tier — ``"user"``, ``"project"``, ``"topic"``,
                     or ``"general"``.  Defaults to ``"general"``.
    :ivar is_passdown: ``True`` when the item is a shift handoff/passdown note.
    :ivar project_tag: JSON-serialized list of project names this item belongs
                       to, or ``None`` if untagged.  A single string is also
                       accepted for backward compat.
    :ivar direction: ``"received"`` (default) or ``"sent"`` — whether the item
                     was received by or sent by the user.
    :ivar conversation_id: Stable thread/conversation identifier from the source
                           system (e.g. Outlook ConversationID).  ``None`` if
                           unavailable.
    :ivar conversation_topic: Cleaned subject / thread title without Re:/Fw:
                               prefixes.  ``None`` if unavailable.
    :ivar goals: Project goals or objectives extracted from the content.
    :ivar key_dates: Deadlines or time references extracted from the content.
    :ivar body_preview: Up to 2000 characters of the item body for re-analysis
                        and keyword learning.
    :ivar to_field: Raw ``To`` header value carried forward from item metadata.
    :ivar cc_field: Raw ``CC`` header value carried forward from item metadata.
    :ivar is_replied: ``True`` when the user has already replied to this item.
    :ivar replied_at: ISO 8601 timestamp of the user's reply, or ``None``.
    :ivar information_items: Factual observations and completed-action notes
                             extracted by the LLM that are not tasks for the user.
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
    task_type:         Optional[str]   = None   # "review" | "reply" | None
    hierarchy:         str             = "general"
    is_passdown:       bool            = False
    project_tag:       Optional[str]   = None
    direction:         str             = "received"   # "received" | "sent"
    conversation_id:   Optional[str]   = None
    conversation_topic: Optional[str]  = None
    goals:             list[str]       = field(default_factory=list)
    key_dates:         list[dict]      = field(default_factory=list)
    body_preview:      str             = ""
    to_field:          str             = ""
    cc_field:          str             = ""
    is_replied:        bool            = False
    replied_at:        Optional[str]   = None
    information_items: list[dict]      = field(default_factory=list)


@dataclass
class Situation:
    """
    A cross-source grouping of related Analysis items.

    :ivar situation_id: Stable UUID for this situation.
    :ivar title: LLM-generated short title.
    :ivar summary: LLM-generated cross-source narrative (2-3 sentences).
    :ivar status: Operational status — ``"blocked"``, ``"in_progress"``,
                  ``"waiting"``, ``"needs_decision"``, or ``"informational"``.
    :ivar item_ids: Ordered list of contributing analysis item_ids.
    :ivar sources: Unique source types present in this situation.
    :ivar project_tag: Shared project tag if all items agree, else ``None``.
    :ivar score: Composite urgency score.
    :ivar priority: Derived from highest-priority contributing item.
    :ivar open_actions: Deduplicated action items across all contributing items.
    :ivar references: Union of all extracted reference strings from members.
    :ivar key_context: LLM-extracted essential background sentence, or ``None``.
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
