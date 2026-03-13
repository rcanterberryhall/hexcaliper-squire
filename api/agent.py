import json
import requests
from models import RawItem, Analysis, ActionItem
import config

PROMPT = """You are a personal ops assistant. Analyze this item and extract any action required from the recipient.

Source: {source}
Title: {title}
From: {author}
Time: {timestamp}
Content:
{body}

Respond ONLY with valid JSON. No explanation, no markdown fences.

{{
  "has_action": true or false,
  "priority": "high" or "medium" or "low",
  "category": one of ["reply_needed", "task", "deadline", "review", "approval", "fyi", "noise"],
  "action_items": [
    {{
      "description": "specific concrete action",
      "deadline": "ISO date string or null",
      "owner": "me or person name"
    }}
  ],
  "summary": "one sentence — what this is and what needs to happen",
  "urgency_reason": "why this priority, or null"
}}

Category guide:
- reply_needed: someone is waiting on a response
- task: concrete work item
- deadline: time-sensitive with explicit due date
- review: PR review or document review
- approval: needs a decision or sign-off
- fyi: informational, no action required
- noise: automated notification, irrelevant

Priority guide:
- high: blocking someone, same-day, or overdue
- medium: due this week or needs response soon
- low: backlog, no deadline pressure

Rules:
- Jira issues: has_action=true unless status is Done/Closed
- GitHub PR review requests: has_action=true, category=review
- Slack DMs: bias toward has_action=true
"""


def analyze(item: RawItem) -> Analysis:
    """Send a single item to Ollama and parse the structured response."""
    response = requests.post(
        config.OLLAMA_URL,
        headers=config.ollama_headers(),
        json={
            "model":   config.OLLAMA_MODEL,
            "prompt":  PROMPT.format(
                source    = item.source,
                title     = item.title,
                author    = item.author,
                timestamp = item.timestamp,
                body      = item.body,
            ),
            "stream":  False,
            "format":  "json",
            "options": {"temperature": 0.1, "num_predict": 512},
        },
        timeout=90,
    )
    response.raise_for_status()

    try:
        data = json.loads(response.json().get("response", "{}"))
    except json.JSONDecodeError:
        data = {}

    action_items = [
        ActionItem(
            description = a.get("description", ""),
            deadline    = a.get("deadline"),
            owner       = a.get("owner", "me"),
        )
        for a in data.get("action_items", [])
        if a.get("description")
    ]

    # Jira fallback — always surface open tickets even if LLM returns sparse output
    if item.source == "jira" and not action_items:
        action_items = [ActionItem(
            description = f"Work on: {item.title}",
            deadline    = item.metadata.get("due"),
            owner       = "me",
        )]

    return Analysis(
        item_id        = item.item_id,
        source         = item.source,
        title          = item.title,
        author         = item.author,
        timestamp      = item.timestamp,
        url            = item.url,
        has_action     = data.get("has_action", bool(action_items)),
        priority       = data.get("priority", "medium"),
        category       = data.get("category", "fyi"),
        action_items   = action_items,
        summary        = data.get("summary", item.title),
        urgency_reason = data.get("urgency_reason"),
    )


def analyze_batch(items: list[RawItem], progress_cb=None) -> list[Analysis]:
    """Analyze a list of items, calling progress_cb(i, total, source, title) after each."""
    results = []
    for i, item in enumerate(items):
        if progress_cb:
            progress_cb(i, len(items), item.source, item.title[:60])
        try:
            results.append(analyze(item))
        except Exception as e:
            print(f"[agent] {item.item_id}: {e}")
    return results
