"""
agent.py — LLM-powered analysis pipeline.

Sends each ``RawItem`` to an Ollama-compatible endpoint and parses the
structured JSON response into an ``Analysis`` object.  The prompt includes
full user context (name, email, active projects with manual and learned
keywords and known senders/groups, watch topics, and noise keywords) so the
model can assign the correct hierarchy tier, project tag, passdown flag,
goals, and key dates in addition to the standard priority/category/action-item
extraction.

Temperature is kept low (0.1) to favour deterministic, structured output.

Key helpers:
    ``_projects_ctx()`` — builds the projects context string for the prompt,
        including learned keywords and known senders/groups per project.
    ``_topics_ctx()``   — builds the watch-topics context string.
    ``_noise_ctx()``    — builds the noise-keywords context string (capped at 30).
    ``extract_emails(text)`` — extracts unique email addresses from a string.
    ``_match_sender(item)`` — checks whether the item's sender or recipient
        addresses match a project's ``learned_senders`` list and returns the
        project name as a prompt hint.  The LLM makes the final call.
    ``_detect_passdown(title, body)`` — deterministic regex pre-check that
        overrides the LLM when the subject or opening lines contain the word
        "passdown" or a phrase like "notes from <shift> shift".
    ``extract_keywords(project_name, title, body)`` — calls the LLM to extract
        5–10 characteristic keywords from an item, used by the project and noise
        learning endpoints in ``app.py``.
"""
import json
import re
import requests
from models import RawItem, Analysis, ActionItem
import config

# ── Prompt template ───────────────────────────────────────────────────────────

PROMPT = """You are a personal ops assistant for {user_name}.

User context:
- Name: {user_name}
- Email: {user_email}
- Active projects: {projects_ctx}
- Watch topics: {topics_ctx}
- Noise/irrelevant topics: {noise_ctx} — if content is primarily about these with no direct relevance to {user_name}, set category="noise", priority="low", has_action=false
{sender_hint}{replied_hint}{manual_tag_hint}{embedding_hint}
Analyze this item and extract structured information.

Source: {source}
Title: {title}
From: {author}
To: {to_field}
CC: {cc_field}
Time: {timestamp}
Content:
{body}

Respond ONLY with valid JSON. No explanation, no markdown fences.

{{
  "has_action": true or false,
  "priority": "high" or "medium" or "low",
  "category": one of ["reply_needed", "task", "deadline", "review", "approval", "fyi", "noise"],
  "hierarchy": one of ["user", "project", "topic", "general"],
  "is_passdown": true or false,
  "project_tag": "exact project name from the active projects list above, or null if none match — do NOT invent names from email content; prefer the most specific sub-project over its parent when both could match",
  "action_items": [
    {{
      "description": "specific concrete action",
      "deadline": "ISO date string or null",
      "owner": "me or person name"
    }}
  ],
  "goals": ["project goal or objective mentioned"],
  "key_dates": [
    {{
      "date": "ISO date, descriptive date, or null",
      "description": "what this date represents"
    }}
  ],
  "information_items": [
    {{
      "fact": "specific piece of information worth noting",
      "relevance": "why this matters — project context, current state, or background"
    }}
  ],
  "summary": "one sentence — what this is and what needs to happen",
  "urgency_reason": "why this priority, or null"
}}

Email recipient rules (apply when To/CC fields are populated):
- {user_name} or {user_email} appears in To → hierarchy=user, direct recipient, bias toward action
- {user_name} or {user_email} appears in CC only:
  * AND body contains @mention of {user_name}/{user_email} or a direct question/request → hierarchy=user, action possible
  * Otherwise → lower priority; hierarchy based on content; category=fyi unless action is explicit
- Neither To nor CC populated, or user absent from both → likely passdown/broadcast; apply passdown rules

Hierarchy — assign the HIGHEST matching tier:
- user: directly addresses {user_name} by name/email; assigned to them; DM to them; uses their @mention
- project: related to one of their active projects but not directly addressed to them
- topic: matches a watch topic but not a specific project
- general: everything else

Passdown — is_passdown=true ONLY when:
- The subject or opening lines contain the word "passdown", OR
- The opening lines contain a phrase like "notes from [shift] shift" (e.g. "notes from 2nd shift", "notes from first shift")
- These are shift-handoff emails from operational teams; they describe ongoing work, current system state, and items to watch
- For passdowns: extract action items ONLY when explicitly directed at {user_name}; otherwise has_action=false

Goals — extract genuine project objectives or milestones stated in the content (not individual tasks).

Key dates — extract any deadlines, release dates, meeting times, or time references (even relative: "end of sprint", "next Monday").

Category rules:
- reply_needed: someone is waiting on a response from {user_name}
- task: work specifically assigned to or requested of {user_name}
- deadline: time-sensitive with an explicit or implied due date
- review: code/doc review requested of {user_name}
- approval: decision or sign-off needed from {user_name}
- fyi: informational, no action required of {user_name}
- noise: automated notifications, irrelevant to {user_name}

Priority rules:
- high: blocking someone, same-day, overdue, or directly requires {user_name}'s immediate action
- medium: needs response this week, or a project item with a near deadline
- low: passdown context, backlog, no deadline pressure

Action item rules:
- owner="me" ONLY when the action is specifically for {user_name}
- An imperative or direct question aimed at {user_name} is required for owner="me"
- Past-tense reports of completed work (e.g. "we installed X", "the issue was resolved") are NOT action items — put them in information_items instead
- Jira issues: has_action=true unless status is Done/Closed
- GitHub PR review requests: has_action=true, category=review
- Slack DMs: bias toward has_action=true

Information item rules:
- Extract key facts, status updates, and completed actions that are worth knowing but are NOT tasks for {user_name}
- Examples: "RV08 seat belt issues resolved", "zip ties were installed on RV5/11/18", "SAT testing scheduled for tomorrow morning"
- If {user_name} is CC'd only and the body describes completed work or a status update → put findings in information_items, not action_items
- Passdown notes are especially rich sources: extract every piece of operational status, equipment state, ongoing concern, or shift observation as a separate information_item
- Do NOT duplicate content across both action_items and information_items
- Leave information_items empty if there is nothing factual worth preserving
"""


def _projects_ctx() -> str:
    """Build a readable summary of configured projects for the prompt."""
    if not config.PROJECTS:
        return "none configured"
    # Index project names for parent lookup
    project_names = {p.get("name") for p in config.PROJECTS}
    parts = []
    for p in config.PROJECTS:
        name   = p.get("name", "unnamed")
        parent = p.get("parent", "")
        desc_text = p.get("description", "")
        kw     = list(p.get("keywords", [])) + list(p.get("learned_keywords", []))
        ch     = ", ".join(p.get("channels", []))
        sr     = list(p.get("senders", [])) + list(p.get("learned_senders", []))

        line = name
        if parent and parent in project_names:
            line += f" [sub-project of {parent}]"
        if desc_text:
            line += f" — {desc_text}"
        if kw:
            line += f" (keywords: {', '.join(kw[:20])})"
        if ch:
            line += f" (channels: {ch})"
        if sr:
            line += f" (known senders: {', '.join(sr[:15])})"
        parts.append(line)
    return "; ".join(parts)


def _topics_ctx() -> str:
    """Build a readable summary of watch topics for the prompt."""
    return ", ".join(config.FOCUS_TOPICS) if config.FOCUS_TOPICS else "none configured"


def _noise_ctx() -> str:
    """Build a summary of learned noise keywords for the prompt."""
    return ", ".join(config.NOISE_KEYWORDS[:30]) if config.NOISE_KEYWORDS else "none"


KEYWORD_PROMPT = """Extract 5 to 10 short keywords or phrases that best characterize this content for project "{project_name}".
Focus on technical terms, system names, product names, process names, and domain-specific concepts.
Avoid generic words like "update", "issue", "please", "team", "message".
Return ONLY a JSON array of strings. No explanation, no markdown fences.

Project: {project_name}
Title: {title}
Content:
{body}
"""


def extract_keywords(project_name: str, title: str, body: str) -> list[str]:
    """
    Ask the LLM to extract keywords from an item for project context learning.

    :param project_name: Name of the project being trained.
    :param title: Item title.
    :param body: Item body text (will be truncated to 2000 chars).
    :return: List of keyword strings, empty list on failure.
    """
    try:
        response = requests.post(
            config.OLLAMA_URL,
            headers=config.ollama_headers(),
            json={
                "model":   config.OLLAMA_MODEL,
                "prompt":  KEYWORD_PROMPT.format(
                    project_name = project_name,
                    title        = title,
                    body         = body[:2000],
                ),
                "stream":  False,
                "format":  "json",
                "options": {"temperature": 0.1, "num_predict": 256},
            },
            timeout=60,
        )
        response.raise_for_status()
        data = json.loads(response.json().get("response", "[]"))
        if isinstance(data, list):
            return [str(k).strip() for k in data if k and len(str(k).strip()) > 2]
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return [str(k).strip() for k in v if k and len(str(k).strip()) > 2]
    except Exception as e:
        print(f"[agent] extract_keywords: {e}")
    return []


import re as _re

_PASSDOWN_PATTERNS = _re.compile(
    r'\bpassdown\b'
    r'|notes from \w+ shift'
    r'|\bshift highlights\b'
    r'|\bshift activities\b'
    r'|\bshift notes\b'
    r'|\bshift report\b'
    r'|\bshift summary\b'
    r'|\bshift handoff\b'
    r'|\bshift handover\b'
    r'|\bshift update\b',
    _re.IGNORECASE,
)
_EMAIL_RE = _re.compile(r'[\w.+\-]+@[\w.\-]+\.[a-z]{2,}', _re.IGNORECASE)


def extract_emails(text: str) -> list[str]:
    """
    Extract unique, lowercase email addresses from a free-form string.

    Covers RFC-style headers such as ``"Name <addr@host.com>"`` as well as
    bare addresses and semicolon/comma-separated lists.

    :param text: Any string that may contain email addresses.
    :type text: str
    :return: Deduplicated list of lowercase email addresses.
    :rtype: list[str]
    """
    return list({m.lower() for m in _EMAIL_RE.findall(text or "")})


def _match_sender(item: RawItem) -> str | None:
    """
    Deterministically match an item to a project via learned sender/group addresses.

    Collects all email addresses from ``item.author``, ``item.metadata["to"]``,
    and ``item.metadata["cc"]``, strips the configured user's own address, then
    checks each project's ``learned_senders`` list for any overlap.

    :param item: The raw item to check.
    :type item: RawItem
    :return: Project name if a sender/group address matches, else ``None``.
    :rtype: str or None
    """
    candidates: set[str] = set()
    for field in (
        item.author,
        item.metadata.get("to", ""),
        item.metadata.get("cc", ""),
    ):
        candidates.update(extract_emails(field))

    # Don't match the user's own address — it appears in nearly every email
    if config.USER_EMAIL:
        candidates.discard(config.USER_EMAIL.lower())

    if not candidates:
        return None

    for p in config.PROJECTS:
        all_senders = list(p.get("senders", [])) + list(p.get("learned_senders", []))
        for sender in all_senders:
            if sender.lower() in candidates:
                return p["name"]
    return None


def _detect_passdown(title: str, body: str) -> bool:
    """
    Deterministically detect shift passdown emails by pattern-matching the
    subject line and the first 300 characters of the body.

    Matches:
    - Any occurrence of the word "passdown" in subject or opening lines
    - Phrases like "notes from 2nd shift" / "notes from first shift"
    """
    return bool(
        _PASSDOWN_PATTERNS.search(title)
        or _PASSDOWN_PATTERNS.search(body[:300])
    )


_CAUTION_PATTERN = re.compile(
    r'CAUTION:\s*This email originated from outside[^\n]*\n'
    r'(?:Do not click[^\n]*\n)?'
    r'(?:\n)*',
    re.IGNORECASE,
)


def _strip_caution(body: str) -> str:
    """Remove the external-sender CAUTION warning from email bodies."""
    return _CAUTION_PATTERN.sub('', body).lstrip()


def _validated_project_tag(tag: str | None) -> str | None:
    """Return tag only if it exactly matches a configured project name, else None."""
    if not tag or not config.PROJECTS:
        return tag
    valid = {p.get("name") for p in config.PROJECTS}
    return tag if tag in valid else None


def analyze(item: RawItem) -> Analysis:
    """
    Send a single item to Ollama and parse the structured JSON response.

    The prompt is built with full user context (name, email, projects, topics,
    noise keywords) and includes the ``to``/``cc`` fields from item metadata so
    the model can apply recipient-based hierarchy rules.

    If the LLM returns malformed JSON, all fields default to safe fallback
    values so the item is still persisted rather than silently dropped.
    Jira items without action items receive an automatic fallback action so
    open tickets are always surfaced.

    ``is_passdown`` is forced to ``True`` when ``_detect_passdown`` matches —
    the only hard deterministic override.  All other classification fields
    (``hierarchy``, ``project_tag``, ``category``, etc.) come from the LLM.
    Sender/group address matches from ``_match_sender`` are passed into the
    prompt as a hint so the model can weigh them against the actual content.
    ``hierarchy`` and ``project_tag`` fall back to values pre-set in
    ``item.metadata`` (e.g. by the Slack connector) when the LLM omits them.

    :param item: The raw item to analyse.
    :type item: RawItem
    :return: Structured analysis result with all enrichment fields populated.
    :rtype: Analysis
    :raises requests.HTTPError: If the Ollama API request fails.
    """
    to_field     = item.metadata.get("to", "")
    cc_field     = item.metadata.get("cc", "")
    is_replied   = bool(item.metadata.get("is_replied", False))
    is_forwarded = bool(item.metadata.get("is_forwarded", False))
    replied_at   = item.metadata.get("replied_at")
    _user_name   = config.USER_NAME or "the user"

    # Build sender hint — tells the LLM which project this sender/group is
    # historically associated with, but does not override LLM classification.
    _sender_match = _match_sender(item)
    if _sender_match:
        sender_hint = (
            f"\n- Sender/recipient hint: past items from this sender or group "
            f"have been tagged to project \"{_sender_match}\". "
            f"Use this as a signal but verify against the content."
        )
    else:
        sender_hint = ""

    # Manual tag hint — when re-analyzing an item the user has already tagged,
    # surface that decision directly so the LLM can reinforce it.
    _manual_tag = item.metadata.get("project_tag")
    if _manual_tag:
        manual_tag_hint = (
            f"\n- Manual project tag: the user has tagged this item to project "
            f"\"{_manual_tag}\". Treat this as a strong signal for project_tag "
            f"and hierarchy assignment."
        )
    else:
        manual_tag_hint = ""

    if is_replied:
        _when = f" (at {replied_at})" if replied_at else ""
        replied_hint = (
            f"\n- Status: {_user_name} has already replied to this email{_when}. "
            f"Lower action urgency unless follow-up work is still clearly pending."
        )
    elif is_forwarded:
        replied_hint = (
            f"\n- Status: {_user_name} has forwarded this email. "
            f"Consider whether further action is still required."
        )
    else:
        replied_hint = ""

    embedding_hint = ""
    body_text = item.body[:2000] or item.title
    if body_text:
        try:
            from embedder import embed, score_item
            vector  = embed(body_text)
            matches = score_item(vector, min_count=3)
            if matches:
                top = matches[0]
                if top["score"] > 0.75:
                    embedding_hint = (
                        f"\n- Embedding classifier hint: this item is semantically similar to "
                        f"past items tagged to project \"{top['project']}\" "
                        f"(category: {top['category']}, confidence: {top['score']:.2f}, "
                        f"based on {top['count']} training items). "
                        f"Use this as a strong signal but verify against content."
                    )
                elif top["score"] > 0.55:
                    embedding_hint = (
                        f"\n- Embedding classifier hint: weak similarity to project "
                        f"\"{top['project']}\" (score: {top['score']:.2f}). "
                        f"Consider but do not rely on this signal."
                    )
        except Exception as e:
            print(f"[agent] embedding score failed: {e}")

    response = requests.post(
        config.OLLAMA_URL,
        headers=config.ollama_headers(),
        json={
            "model":   config.OLLAMA_MODEL,
            "prompt":  PROMPT.format(
                source       = item.source,
                title        = item.title,
                author       = item.author,
                timestamp    = item.timestamp,
                body         = item.body,
                user_name    = config.USER_NAME or "the user",
                user_email   = config.USER_EMAIL or "",
                projects_ctx = _projects_ctx(),
                topics_ctx   = _topics_ctx(),
                noise_ctx    = _noise_ctx(),
                to_field     = to_field,
                cc_field     = cc_field,
                sender_hint     = sender_hint,
                replied_hint    = replied_hint,
                manual_tag_hint = manual_tag_hint,
                embedding_hint  = embedding_hint,
            ),
            "stream":  False,
            "format":  "json",
            "options": {"temperature": 0.1, "num_predict": 768},
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

    # Jira fallback — always surface open tickets even if the LLM returns sparse output.
    if item.source == "jira" and not action_items:
        action_items = [ActionItem(
            description = f"Work on: {item.title}",
            deadline    = item.metadata.get("due"),
            owner       = "me",
        )]

    information_items = [
        {"fact": i.get("fact", ""), "relevance": i.get("relevance", "")}
        for i in data.get("information_items", [])
        if i.get("fact")
    ]

    return Analysis(
        item_id           = item.item_id,
        source            = item.source,
        title             = item.title,
        author            = item.author,
        timestamp         = item.timestamp,
        url               = item.url,
        has_action        = data.get("has_action", bool(action_items)),
        priority          = data.get("priority", "medium"),
        category          = data.get("category", "fyi"),
        action_items      = action_items,
        summary           = data.get("summary", item.title),
        urgency_reason    = data.get("urgency_reason"),
        hierarchy         = data.get("hierarchy", item.metadata.get("hierarchy", "general")),
        is_passdown       = _detect_passdown(item.title, item.body) or bool(data.get("is_passdown", False)),
        project_tag       = _validated_project_tag(
                               data.get("project_tag") or item.metadata.get("project_tag")
                           ),
        goals             = [g for g in data.get("goals", []) if isinstance(g, str) and g],
        key_dates         = [d for d in data.get("key_dates", []) if isinstance(d, dict)],
        body_preview      = _strip_caution(item.body)[:2000],
        to_field          = to_field,
        cc_field          = cc_field,
        is_replied        = is_replied,
        replied_at        = replied_at,
        information_items = information_items,
    )


def analyze_batch(items: list[RawItem], progress_cb=None) -> list[Analysis]:
    """
    Analyse a list of items sequentially, with optional progress reporting.

    Failed items are logged and skipped rather than aborting the batch, so a
    single Ollama timeout does not prevent the remaining items from being
    processed.

    :param items: List of raw items to analyse.
    :type items: list[RawItem]
    :param progress_cb: Optional callback invoked after each item with the
                        signature ``(index, total, source, title)``.
    :type progress_cb: callable, optional
    :return: List of analysis results for all successfully processed items.
    :rtype: list[Analysis]
    """
    results = []
    for i, item in enumerate(items):
        if progress_cb:
            progress_cb(i, len(items), item.source, item.title[:60])
        try:
            results.append(analyze(item))
        except Exception as e:
            print(f"[agent] {item.item_id}: {e}")
    return results
