"""
models.py — Core data models for the Squire analysis pipeline.

Defines the two-stage data flow:

1. ``RawItem`` — a normalised, source-agnostic representation of an inbound
   item (email, Slack message, GitHub notification, Jira issue) before AI
   processing.
2. ``Analysis`` — the structured result produced by the LLM for a single
   ``RawItem``, including priority, category, action items, and a summary.
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
