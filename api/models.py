from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawItem:
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
    description: str
    deadline:    Optional[str]
    owner:       str


@dataclass
class Analysis:
    item_id:        str
    source:         str
    title:          str
    author:         str
    timestamp:      str
    url:            str
    has_action:     bool
    priority:       str   # high | medium | low
    category:       str   # reply_needed | task | deadline | review | approval | fyi | noise
    action_items:   list[ActionItem]
    summary:        str
    urgency_reason: Optional[str]
