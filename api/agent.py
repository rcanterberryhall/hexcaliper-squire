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
        including ``description``, ``parent`` relationship, learned keywords,
        channels, and known senders/groups per project (up to 20 keywords and
        15 senders per project).
    ``_topics_ctx()``   — builds the watch-topics context string.
    ``_noise_ctx()``    — builds the noise-keywords context string (capped at 30).
    ``extract_emails(text)`` — extracts unique email addresses from a string.
    ``_match_sender(item)`` — checks whether the item's sender or recipient
        addresses match a project's ``senders`` or ``learned_senders`` list
        and returns the project name as a prompt hint.  The LLM makes the
        final call.
    ``_detect_passdown(title, body)`` — deterministic regex pre-check that
        overrides the LLM when the subject or opening lines match
        ``_PASSDOWN_PATTERNS`` (covers "passdown", "notes from X shift",
        "shift highlights/activities/notes/report/summary/handoff/update").
    ``_strip_caution(body)`` — removes the standardised external-sender
        CAUTION warning header from email bodies before storage and prompt
        construction (matched by ``_CAUTION_PATTERN``).
    ``_validated_project_tags(tags)`` — validates LLM-returned project tag(s)
        against the configured project list, returning ``None`` for invented
        names.
    ``extract_keywords(project_name, title, body)`` — calls the LLM to extract
        5–10 characteristic keywords from an item, used by the project and noise
        learning endpoints in ``app.py``.
"""
import json
import logging
import re
import requests
from models import RawItem, Analysis, ActionItem
import config
import db
import llm

log = logging.getLogger(__name__)

# ── Prompt template ───────────────────────────────────────────────────────────

PROMPT = """You are a personal ops assistant for {user_name}.

User context:
- Name: {user_name}
- Email: {user_email}
- Active projects: {projects_ctx}
- Watch topics: {topics_ctx}
- Assignment corrections (learn from these past mistakes): {assignment_corrections_ctx}
- Priority overrides (user has manually corrected these priority calls — weight these strongly): {priority_overrides_ctx}
- Task indicators: {task_ctx} — keywords from items previously confirmed as tasks requiring action from {user_name}
- Approval indicators: {approval_ctx} — keywords from items previously confirmed as approval events affecting {user_name}
- FYI indicators: {fyi_ctx} — keywords from items previously confirmed as informational only for {user_name}
- Noise/irrelevant topics: {noise_ctx} — if content is primarily about these with no direct relevance to {user_name}, set category="noise", priority="low", has_action=false
{sender_hint}{replied_hint}{manual_tag_hint}{graph_hint}{embedding_hint}{recipient_scope_hint}{thread_todos_hint}
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
  "category": one of ["task", "approval", "fyi", "noise"],
  "task_type": one of ["reply", "review", null],
  "hierarchy": one of ["user", "project", "topic", "general"],
  "is_passdown": true or false,
  "project_tags": ["exact project name(s) from the active projects list above — one or more tags allowed; use an empty list [] if none match; do NOT invent names; prefer the most specific sub-project, but if the item spans multiple projects (e.g. a passdown covering several sub-projects), list ALL relevant projects"],
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
  "summary": "one sentence — what this is and what needs to happen. NEVER repeat the subject line verbatim; if body is empty or trivial, write 'No actionable content.'",
  "urgency_reason": "why this priority, or null"
}}

Email recipient rules (apply when To/CC fields are populated):
- Use the "Recipient scope" hint above (if present) as the primary signal for how broadly this message is addressed.
- direct (1:1 to {user_name}): hierarchy=user; directives in the body are strongly biased toward action for {user_name}.
- small group (2–4 visible recipients): directives may be for {user_name} or another named recipient — apply the action item rules to decide.
- group (5–10) or broadcast (11+ or distribution list): do NOT default to "for {user_name}" just because {user_name} is in To or CC. A directive is only for {user_name} when explicitly named, @mentioned, or asked a direct question. Still capture directives aimed at OTHER named people as action_items with owner=<that person>.
- {user_name} is in CC only: presence in CC means awareness, not ownership. Lower priority unless the body contains an explicit @mention or direct question to {user_name}.
- {user_name} absent from visible To and CC → received via distribution list or BCC; treat as broadcast.

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
- Passdowns are NEVER category="noise" — they are operational documents. Use category="fyi" unless a specific task for {user_name} is called out
- When a passdown covers multiple sub-projects, list ALL relevant projects in project_tags (e.g. ["P905", "Seatbelts upgrades", "Transformer upgrade"])

Goals — extract genuine project objectives or milestones stated in the content (not individual tasks).

Key dates — extract any deadlines, release dates, meeting times, or time references (even relative: "end of sprint", "next Monday").

Category rules:
- task: actionable work for {user_name}; refine with task_type:
  * task_type="reply"  — someone is waiting on a response from {user_name}
  * task_type="review" — code/doc review, approval, or sign-off needed from {user_name}
  * task_type=null     — general assigned work, deadline, or other task
- approval: an approval event that already occurred or affects {user_name} (PO approved, plan signed off, timesheets approved) — NOT a request for approval (use task/review for that)
- fyi: informational only — no action required of {user_name}; MUST have has_action=false and action_items=[]; use as the default when no strong indicators exist for task, approval, or noise
- noise: automated notifications, irrelevant to {user_name}; MUST have has_action=false and action_items=[]

Priority rules:
- high: blocking someone, same-day, overdue, or directly requires {user_name}'s immediate action
- medium: needs response this week, or a project item with a near deadline
- low: passdown context, backlog, no deadline pressure

Action item rules:
- Tracking work delegated to OTHER PEOPLE is a primary goal, not an afterthought. A directive aimed at a NAMED person in the body ("Mike, please pull the drawings", "Can Sarah review this", "John to handle the migration by Friday") is ALWAYS an action_item with owner=<that named person> — regardless of recipient scope, regardless of whether {user_name} is in To/CC, regardless of whether the named person is in To/CC.
- Assign owner="me" ONLY when at least one of the following is true:
  1. The action is explicitly directed at {user_name} by name, email, @mention, or direct question ("Alice, please review", "@alice can you confirm", "Alice — thoughts?")
  2. Recipient scope is "direct" (1:1 message to {user_name}) AND the body contains a clear directive
- Do NOT assign owner="me" just because {user_name} appears in To or CC of a group or broadcast message.
- Being in CC means awareness, not ownership — never default owner="me" from CC alone.
- Use the person's full name as it appears in the To/CC header (e.g. "John Johnson"), not a first-name guess, whenever possible.
- If a directive has no identifiable owner (no name, no @mention, no direct address) and recipient scope is not "direct", OMIT the action_item rather than guessing.
- Do NOT assign owner="me" on the basis of the user's name appearing in any of these contexts — they are NOT directives to {user_name}:
  * A quoted reply chain or forwarded-message header below the current author's message (anything after "-----Original Message-----", "From: …", or "On <date>, <X> wrote:").
  * An @mention of {user_name} as a third-party to coordinate with (e.g. "…to help coordinate with @{user_name}'s team", "@{user_name}'s crew will handle X").
  * A tracking URL, disclaimer footer, or "Individualized for <email>" marker.
  These mentions place {user_name} in the text but do not address them.
- Past-tense reports of completed work ("we installed X", "the issue was resolved") are NOT action items — put them in information_items instead.
- Jira issues: has_action=true unless status is Done/Closed; owner="me" (these are pre-assigned).
- GitHub PR review requests: has_action=true, category=review, owner="me".
- Slack DMs: recipient scope is always "direct"; bias toward has_action=true.

Information item rules:
- Extract key facts, status updates, and completed actions that are worth knowing but are NOT tasks for {user_name}
- Examples: "RV08 seat belt issues resolved", "zip ties were installed on RV5/11/18", "SAT testing scheduled for tomorrow morning"
- If {user_name} is CC'd only and the body describes completed work or a status update with no directives → put findings in information_items, not action_items.  (Directives aimed at named people in the same body still become action_items with owner=<that person>.)
- Passdown notes are especially rich sources: extract every piece of operational status, equipment state, ongoing concern, or shift observation as a separate information_item
- Do NOT duplicate content across both action_items and information_items
- Leave information_items empty if there is nothing factual worth preserving
"""


def _projects_ctx() -> str:
    """
    Build a readable summary of all configured projects for the LLM prompt.

    For each project the output line includes:
    - ``name`` — always included.
    - ``[sub-project of <parent>]`` — appended when ``parent`` is set and
      matches an existing project name (validates the relationship).
    - ``— <description>`` — appended when a ``description`` is set.
    - ``(keywords: ...)`` — combined ``keywords`` + ``learned_keywords``,
      capped at 20.
    - ``(channels: ...)`` — Slack/Teams channel names from ``channels``.
    - ``(known senders: ...)`` — combined ``senders`` + ``learned_senders``,
      capped at 15.

    :return: Semicolon-separated project summaries, or ``"none configured"``
             if ``config.PROJECTS`` is empty.
    :rtype: str
    """
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
    """
    Build a comma-separated summary of watch topics for the LLM prompt.

    :return: Comma-separated ``config.FOCUS_TOPICS``, or ``"none configured"``.
    :rtype: str
    """
    return ", ".join(config.FOCUS_TOPICS) if config.FOCUS_TOPICS else "none configured"


def _noise_ctx() -> str:
    """
    Build a comma-separated summary of noise keywords for the LLM prompt.

    Capped at 30 keywords to keep the prompt size reasonable.

    :return: Comma-separated noise keywords, or ``"none"`` if the list is empty.
    :rtype: str
    """
    return ", ".join(config.NOISE_KEYWORDS[:30]) if config.NOISE_KEYWORDS else "none"


def _assignment_corrections_ctx() -> str:
    """
    Build a readable summary of past assignment corrections for the LLM prompt.

    Each correction records what the LLM originally inferred as the owner and
    what the user corrected it to.  Injected as few-shot examples so the model
    learns to assign ownership more accurately over time.

    Capped at 20 corrections to keep the prompt size reasonable.

    :return: Newline-separated correction examples, or ``"none"`` if empty.
    :rtype: str
    """
    corrections = config.ASSIGNMENT_CORRECTIONS[-20:]
    if not corrections:
        return "none"
    lines = []
    for c in corrections:
        desc    = c.get("description", "")
        llm_own = c.get("llm_owner") or "me"
        correct = c.get("corrected_to", "")
        lines.append(f'  - "{desc}": LLM assigned to "{llm_own}", user corrected to "{correct}"')
    return "\n" + "\n".join(lines)


def _priority_overrides_ctx() -> str:
    """
    Build a readable summary of user priority overrides for the LLM prompt.

    Each override records an item whose LLM-assigned priority the user manually
    changed, along with a one-click reason.  Grouped by reason so the model can
    learn the underlying pattern (a specific person matters, a topic is hot, a
    real deadline exists, etc.) rather than memorising individual items.

    Capped at 25 most recent overrides to keep the prompt size reasonable.

    :return: Grouped-by-reason override summary, or ``"none"`` if empty.
    :rtype: str
    """
    overrides = config.PRIORITY_OVERRIDES[-25:]
    if not overrides:
        return "none"
    by_reason: dict[str, list[dict]] = {}
    for o in overrides:
        by_reason.setdefault(o.get("reason", "other"), []).append(o)
    lines = []
    for reason, entries in by_reason.items():
        lines.append(f'  - Reason: {reason}')
        for o in entries[-8:]:  # cap per-reason detail
            author = o.get("author") or "unknown sender"
            tag    = o.get("project_tag") or ""
            llm_p  = o.get("llm_priority") or "?"
            user_p = o.get("user_priority") or "?"
            title  = (o.get("title") or "")[:80]
            tag_part = f' [{tag}]' if tag else ""
            lines.append(
                f'    · "{title}" from {author}{tag_part}: '
                f'LLM said {llm_p}, user set {user_p}'
            )
    return "\n" + "\n".join(lines)


def _task_ctx() -> str:
    """
    Build a comma-separated summary of learned task keywords for the LLM prompt.

    :return: Comma-separated task keywords, or ``"none"`` if the list is empty.
    :rtype: str
    """
    return ", ".join(config.TASK_KEYWORDS[:30]) if config.TASK_KEYWORDS else "none"


def _approval_ctx() -> str:
    """
    Build a comma-separated summary of learned approval keywords for the LLM prompt.

    :return: Comma-separated approval keywords, or ``"none"`` if the list is empty.
    :rtype: str
    """
    return ", ".join(config.APPROVAL_KEYWORDS[:30]) if config.APPROVAL_KEYWORDS else "none"


def _fyi_ctx() -> str:
    """
    Build a comma-separated summary of learned FYI keywords for the LLM prompt.

    :return: Comma-separated FYI keywords, or ``"none"`` if the list is empty.
    :rtype: str
    """
    return ", ".join(config.FYI_KEYWORDS[:30]) if config.FYI_KEYWORDS else "none"


BRIEFING_PROMPT = """/no_think
You are a personal ops assistant for {user_name}.
Write a 2-3 sentence status briefing for the project "{project_name}".

Recent intel and status updates:
{intel_facts}

Active situations:
{situations}

Open action items:
{action_items}

Write a concise paragraph that tells {user_name} where this project stands right now, what is actively in progress, and what needs attention. Be specific — reference concrete items. Do not use filler phrases like "it is important to note" or "in summary". Do NOT include any reasoning, thinking, or planning — output ONLY the final briefing paragraph. Return plain text only, no markdown.
"""


def generate_project_briefing(
    project_name: str,
    intel_facts:  list[str],
    situations:   list[str],
    action_items: list[str],
) -> str:
    """
    Ask the LLM to write a 2-3 sentence status paragraph for a project.

    :param project_name: Name of the project (or ``"General"`` for untagged).
    :param intel_facts:  Recent intel fact strings for this project.
    :param situations:   Active situation title + status strings.
    :param action_items: Open action item description strings.
    :return: Prose status paragraph, or empty string on failure.
    :rtype: str
    """
    def _fmt(items: list[str], limit: int) -> str:
        return "\n".join(f"- {i}" for i in items[:limit]) if items else "- (none)"

    try:
        text = llm.generate(
            BRIEFING_PROMPT.format(
                user_name    = config.USER_NAME or "the user",
                project_name = project_name,
                intel_facts  = _fmt(intel_facts,  10),
                situations   = _fmt(situations,    5),
                action_items = _fmt(action_items,  8),
            ),
            format=None, temperature=0.3, num_predict=512, timeout=60,
            priority="feedback",
        )
        return text.strip()
    except Exception as e:
        log.error("generate_project_briefing(%s): %s", project_name, e)
        return ""


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
        text = llm.generate(
            KEYWORD_PROMPT.format(
                project_name = project_name,
                title        = title,
                body         = body[:2000],
            ),
            format="json", temperature=0.1, num_predict=256, timeout=60,
            priority="short",
        )
        data = json.loads(text or "[]")
        if isinstance(data, list):
            return [str(k).strip() for k in data if k and len(str(k).strip()) > 2]
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return [str(k).strip() for k in v if k and len(str(k).strip()) > 2]
    except Exception as e:
        log.error("extract_keywords: %s", e)
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

# Matches the author field of Microsoft 365 quarantine digest emails.
_QUARANTINE_AUTHOR_RE = _re.compile(r'quarantine@.*\.microsoft\.com', _re.IGNORECASE)

# Extracts the quarantined sender line from the body, e.g. "Sender:   foo@bar.com"
_QUARANTINE_SENDER_RE = _re.compile(r'Sender:\s+([\w.+\-]+@[\w.\-]+\.[a-z]{2,})', _re.IGNORECASE)


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


# Matches RFC-style "Display Name <email@host>" pairs.
_NAME_EMAIL_RE = _re.compile(r'([^<;,]+?)\s*<([\w.+\-]+@[\w.\-]+\.[a-z]{2,})>', _re.IGNORECASE)


# Distribution-list and group-alias patterns — if any address in To/CC matches,
# the message is classified as broadcast regardless of recipient count.  Covers
# common conventions like ``all-hands@``, ``dl-engineering@``, ``eng-team@``,
# ``everyone@``, and dedicated list domains such as ``*@lists.company.com``.
_DL_LOCAL_RE  = _re.compile(
    r"^(?:all[-_.]|dl[-_.]|everyone$|team$|[\w.\-]+[-_.](?:team|list|group|all))",
    _re.IGNORECASE,
)
_DL_DOMAIN_RE = _re.compile(r"@(?:lists|groups|mailman)\.", _re.IGNORECASE)


def _is_distribution_list(email: str) -> bool:
    """
    Heuristically decide whether an email address is a distribution list
    or group alias rather than a personal mailbox.

    :param email: Lowercased email address.
    :return: ``True`` if the address matches a known group-alias pattern.
    :rtype: bool
    """
    if not email or "@" not in email:
        return False
    local = email.split("@", 1)[0]
    return bool(_DL_LOCAL_RE.match(local)) or bool(_DL_DOMAIN_RE.search(email))


def compute_recipient_scope(user_email: str, to_field: str, cc_field: str) -> dict:
    """
    Classify how broadly an email is addressed, from the user's perspective.

    Returns a dict with:
      - ``scope``: one of ``"direct"``, ``"small"``, ``"group"``, ``"broadcast"``
      - ``to_count`` / ``cc_count`` / ``total``: unique visible address counts
      - ``dls``: list of distribution-list addresses found in To/CC
      - ``user_in_to`` / ``user_in_cc``: whether the user is a visible recipient

    Tiers:
      - ``direct``    — exactly 1 visible address (To) and it is the user
      - ``small``     — 2–4 visible addresses total
      - ``group``     — 5–10 visible addresses total
      - ``broadcast`` — 11+ addresses, any distribution list detected, or the
                        user is not a visible recipient (received via list/BCC)

    Sources without populated headers (Jira, GitHub, Slack DMs) get
    ``total=0`` and ``scope="direct"`` so the downstream rules treat them as
    already-targeted.

    :param user_email: Configured user email (may be empty).
    :param to_field:   Raw ``To`` header value.
    :param cc_field:   Raw ``CC`` header value.
    :return: Scope classification dict.
    :rtype: dict
    """
    to_emails = extract_emails(to_field)
    cc_emails = extract_emails(cc_field)
    ue = (user_email or "").lower().strip()

    user_in_to = ue in to_emails if ue else False
    user_in_cc = ue in cc_emails if ue else False

    all_emails = set(to_emails) | set(cc_emails)
    dls        = sorted(e for e in all_emails if _is_distribution_list(e))
    total      = len(all_emails)

    if total == 0:
        scope = "direct"
    elif dls or total >= 11:
        scope = "broadcast"
    elif ue and not user_in_to and not user_in_cc:
        # User isn't a visible recipient — likely reached them via a list or BCC.
        scope = "broadcast"
    elif total == 1 and user_in_to:
        scope = "direct"
    elif total <= 4:
        scope = "small"
    elif total <= 10:
        scope = "group"
    else:
        scope = "broadcast"

    return {
        "scope":      scope,
        "to_count":   len(to_emails),
        "cc_count":   len(cc_emails),
        "total":      total,
        "dls":        dls,
        "user_in_to": user_in_to,
        "user_in_cc": user_in_cc,
    }


def _recipient_scope_hint(scope_info: dict, user_name: str) -> str:
    """
    Build a prompt hint paragraph describing recipient scope for the LLM.

    Returns an empty string when there are no visible recipients (Jira,
    GitHub, Slack DMs) so the prompt stays clean for non-email sources.

    :param scope_info: Result of :func:`compute_recipient_scope`.
    :param user_name:  Configured user display name.
    :return: Prompt hint line (starts with ``\\n-``) or empty string.
    :rtype: str
    """
    if scope_info["total"] == 0:
        return ""

    scope = scope_info["scope"]
    total = scope_info["total"]
    dls   = scope_info["dls"]

    if scope == "direct":
        body = (
            f"This is a direct 1:1 message to {user_name}. "
            f"Directives in the body are strongly likely to be for {user_name}."
        )
    elif scope == "small":
        body = (
            f"Small-group message ({total} visible recipients). "
            f"A directive may be for {user_name} or another named recipient — "
            f"apply the action item ownership rules to decide."
        )
    elif scope == "group":
        body = (
            f"Group message ({total} visible recipients). "
            f"A directive in the body is NOT automatically for {user_name}. "
            f'Only assign owner="me" when {user_name} is named, @mentioned, '
            f"or asked a direct question; otherwise capture the action_item "
            f"with owner=<the person the directive addresses> or omit it."
        )
    else:  # broadcast
        dl_note = f" (includes distribution list: {', '.join(dls[:3])})" if dls else ""
        body = (
            f"Broadcast message ({total} visible recipients{dl_note}). "
            f"A directive in the body is very unlikely to be a personal task "
            f'for {user_name}. Only assign owner="me" when {user_name} is '
            f"named explicitly, @mentioned, or asked a direct question. Still "
            f"capture any directive clearly aimed at a named person as an "
            f"action_item with owner=<that person>."
        )

    return f"\n- Recipient scope: {scope}. {body}"


def resolve_owner_email(owner: str, *header_fields: str) -> str | None:
    """
    Try to resolve a person's name to an email address.

    First scans the supplied To/CC header fields for ``"Display Name <email>"``
    pairs whose display name contains ``owner`` as a case-insensitive
    substring.  When that fails, falls back to the master contacts table —
    important for delegated directives like "Mike, pull the drawings" in an
    email where Mike isn't one of the visible recipients.

    :param owner: Person name returned by the LLM (e.g. ``"John Johnson"``).
    :param header_fields: One or more raw To/CC header strings.
    :return: Matched email address (lowercase), or ``None`` if no match found.
    :rtype: str or None
    """
    owner_lower = owner.lower().strip()
    if not owner_lower:
        return None

    for field in header_fields:
        for match in _NAME_EMAIL_RE.finditer(field or ""):
            display_name = match.group(1).strip()
            email        = match.group(2).lower()
            if owner_lower in display_name.lower():
                return email

    # Fallback: look up the name in the contacts table.  Imported lazily to
    # avoid a circular import (db imports nothing from agent, but contacts.py
    # imports from agent — keeping the call here local protects future
    # refactors from accidentally inverting that dependency).
    try:
        import db as _db
        matches = _db.find_contacts_by_name(owner_lower)
    except Exception:                                       # pragma: no cover
        return None
    for contact in matches:
        primary = contact.get("primary_email")
        if primary:
            return primary
    return None


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
    Deterministically detect shift handoff emails via ``_PASSDOWN_PATTERNS``.

    Checks the item's title (subject line) and the first 300 characters of
    the body.  A match forces ``is_passdown=True`` in the final ``Analysis``
    regardless of what the LLM returns.

    Patterns matched (case-insensitive, via ``_PASSDOWN_PATTERNS``):
    - ``"passdown"`` (whole word)
    - ``"notes from <word> shift"``
    - ``"shift highlights"``
    - ``"shift activities"``
    - ``"shift notes"``
    - ``"shift report"``
    - ``"shift summary"``
    - ``"shift handoff"``
    - ``"shift handover"``
    - ``"shift update"``

    :param title: Item title or subject line.
    :type title: str
    :param body: Full item body text.
    :type body: str
    :return: ``True`` if any passdown pattern matches.
    :rtype: bool
    """
    return bool(
        _PASSDOWN_PATTERNS.search(title)
        or _PASSDOWN_PATTERNS.search(body[:300])
    )


def _detect_quarantine_noise(item: "RawItem") -> bool:
    """
    Deterministically force noise for Microsoft 365 quarantine digest emails
    whose quarantined sender is not associated with any configured project.

    Returns ``True`` (→ force noise) when:
    - The item author matches ``_QUARANTINE_AUTHOR_RE`` (a quarantine digest), AND
    - The body contains a ``Sender:`` line, AND
    - That sender address does not appear in any project's ``senders`` or
      ``learned_senders`` list.

    Returns ``False`` when the quarantined sender is a known project sender,
    allowing normal analysis to proceed so the item surfaces as usual.

    :param item: The raw item to check.
    :type item: RawItem
    :return: ``True`` if the item should be forced to noise.
    :rtype: bool
    """
    if not _QUARANTINE_AUTHOR_RE.search(item.author or ""):
        return False

    match = _QUARANTINE_SENDER_RE.search(item.body or "")
    if not match:
        # Quarantine notification but no extractable sender — treat as noise.
        return True

    quarantined_sender = match.group(1).lower()

    for p in config.PROJECTS:
        all_senders = list(p.get("senders", [])) + list(p.get("learned_senders", []))
        if quarantined_sender in {s.lower() for s in all_senders}:
            return False

    return True


_CAUTION_PATTERN = re.compile(
    r'CAUTION:\s*This email originated from outside[^\n]*\n'
    r'(?:Do not click[^\n]*\n)?'
    r'(?:\n)*',
    re.IGNORECASE,
)


def _strip_caution(body: str) -> str:
    """
    Remove the standard external-sender CAUTION banner from an email body.

    Many mail systems prepend a boilerplate warning such as::

        CAUTION: This email originated from outside the organization.
        Do not click links or open attachments unless you recognize...

    This banner adds noise to the LLM prompt and the stored body preview.
    ``_CAUTION_PATTERN`` matches the banner and an optional "Do not click"
    line, stripping them before the text is used further.

    :param body: Raw email body text.
    :type body: str
    :return: Body text with the CAUTION banner removed and leading whitespace
             stripped.
    :rtype: str
    """
    return _CAUTION_PATTERN.sub('', body).lstrip()


def _validated_project_tags(tags) -> list[str]:
    """
    Validate LLM-returned project tags against the configured project list.

    Accepts a single string, a list of strings, or None.  Returns a list of
    validated project names (only those that exist in ``config.PROJECTS``).
    When ``config.PROJECTS`` is empty, all tags pass through.

    :param tags: Project name(s) returned by the LLM.
    :return: List of validated project names (may be empty).
    :rtype: list[str]
    """
    if not tags:
        return []
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        return []
    if not config.PROJECTS:
        return [t for t in tags if isinstance(t, str) and t]
    valid = {p.get("name") for p in config.PROJECTS}
    return [t for t in tags if isinstance(t, str) and t in valid]


def _render_thread_todos_hint(thread_todos: list[dict] | None) -> str:
    """Render the prior-message todos prompt block for parsival#79.

    Empty or None → empty string (no hint), preserving prompt shape for
    first-in-thread messages and non-email sources.
    """
    if not thread_todos:
        return ""
    lines = []
    for t in thread_todos:
        desc = (t.get("description") or "").strip()
        if not desc:
            continue
        owner = (t.get("owner") or "").strip() or "unassigned"
        deadline = t.get("deadline") or "no deadline"
        lines.append(f"  - {desc} (owner: {owner}, due: {deadline})")
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "\n- Already tracked in this email thread — do NOT re-emit these as "
        "new action items, even with rewording; still emit any genuinely new "
        "tasks introduced by this message.\n" + body
    )


# ── Body cleaning for LLM prompt (issue #83) ──────────────────────────────────
#
# The raw Outlook body often contains a quoted reply chain and SafeLinks
# tracking URLs decorated with the user's email as a parameter.  Both artifacts
# smuggle the user's name/email into the prompt in positions that are NOT
# directives to the user, which made the LLM assign owner="me" for tasks
# actually aimed at other recipients (issue #83).  These helpers produce a
# cleaned "current message only" body for LLM input; the original body is
# still stored in items.body_preview and fed to the embedding hint.

#: SafeLinks URLs — Outlook rewrites every external link so the URL contains
#: the user's email under &data=... .  We drop the whole URL; if the author
#: wrote a plain-text link adjacent, it is preserved.
_SAFELINKS_RE = re.compile(
    r"https://[A-Za-z0-9.-]*safelinks\.protection\.outlook\.com/[^\s>)]*",
    re.IGNORECASE,
)


def _strip_quoted_reply_tail(body: str) -> str:
    """
    Truncate ``body`` at the first quoted-reply marker line.

    Reuses :data:`signatures._QUOTE_MARKERS` so the set of markers stays in
    one place.  Conservative: only hard boundaries (``-----Original
    Message-----``, ``From:`` header start-of-line, ``On <date> ... wrote:``,
    ``> `` prefix) trigger the cut.  Inline paraphrases of earlier messages
    are kept.

    :param body: Raw message body.
    :return: Body truncated at the first quoted-reply marker, or the full
             body if none matches.
    """
    if not body:
        return body
    from signatures import _QUOTE_MARKERS  # deferred: signatures imports agent at module scope
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        for marker in _QUOTE_MARKERS:
            if marker.match(line):
                return "\n".join(lines[:idx]).rstrip()
    return body


_SAFELINKS_TRAIL_RE = re.compile(r"[.,!?:;]+$")


def _strip_safelinks(body: str) -> str:
    """
    Drop Outlook SafeLinks tracking URLs from ``body``.

    These URLs carry the user's email in the ``&data=`` parameter and trip
    naive "is the user named in this email" checks.  Matching stops at
    whitespace, ``>``, or ``)``.  Any trailing sentence punctuation captured
    at the end of the match is returned to the output so the surrounding
    text is not damaged.
    """
    if not body:
        return body

    def _replace(m: re.Match) -> str:
        url = m.group(0)
        trail = _SAFELINKS_TRAIL_RE.search(url)
        return trail.group(0) if trail else ""

    return _SAFELINKS_RE.sub(_replace, body)


def _clean_body_for_llm(body: str) -> str:
    """
    Produce the cleaned body fed to the LLM for analysis.

    Composition: strip SafeLinks first (so URL fragments in the reply chain
    cannot survive the chain cut as mid-line remnants), then strip the
    quoted reply tail.  Idempotent.
    """
    return _strip_quoted_reply_tail(_strip_safelinks(body))


def build_prompt(item: RawItem, *, thread_todos: list[dict] | None = None) -> str:
    """
    Build the LLM analysis prompt for an item without submitting it.

    Returns the fully formatted prompt string that ``analyze`` would send to
    Ollama.  Used by the batch submission path in ``orchestrator.py`` to
    construct the prompt when routing through merLLM's batch API.

    :param item: The raw item to build a prompt for.
    :type item: RawItem
    :param thread_todos: Open todos already saved for strictly-earlier items
        in the same ``conversation_id``, rendered as a "do not re-emit" hint
        to suppress paraphrased duplicates across reply chains (parsival#79).
    :return: Fully formatted prompt string.
    :rtype: str
    """
    to_field     = item.metadata.get("to", "")
    cc_field     = item.metadata.get("cc", "")
    is_replied   = bool(item.metadata.get("is_replied", False))
    is_forwarded = bool(item.metadata.get("is_forwarded", False))
    replied_at   = item.metadata.get("replied_at")
    _user_name   = config.USER_NAME or "the user"

    _sender_match = _match_sender(item)
    if _sender_match:
        sender_hint = (
            f"\n- Sender/recipient hint: past items from this sender or group "
            f"have been tagged to project \"{_sender_match}\". "
            f"Use this as a signal but verify against the content."
        )
    else:
        sender_hint = ""

    _manual_tag = item.metadata.get("project_tag")
    if _manual_tag:
        manual_tag_hint = (
            f"\n- Manual project tag: the user has tagged this item to project "
            f"\"{_manual_tag}\". Treat this as a strong signal for project_tag "
            f"and hierarchy assignment. Include it in project_tags."
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

    graph_hint = ""
    try:
        import graph as _graph
        ctx_items = _graph.get_context(item, max_n=4)
        ctx_text  = _graph.format_context(ctx_items)
        if ctx_text:
            graph_hint = f"\n- {ctx_text}"
    except Exception as e:
        log.warning("graph context failed: %s", e)

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
            log.warning("embedding score failed: %s", e)

    scope_info          = compute_recipient_scope(config.USER_EMAIL or "", to_field, cc_field)
    recipient_scope_hint = _recipient_scope_hint(
        scope_info, config.USER_NAME or "the user"
    )
    thread_todos_hint = _render_thread_todos_hint(thread_todos)

    return PROMPT.format(
        source       = item.source,
        title        = item.title,
        author       = item.author,
        timestamp    = item.timestamp,
        body         = _clean_body_for_llm(item.body),
        user_name    = config.USER_NAME or "the user",
        user_email   = config.USER_EMAIL or "",
        projects_ctx  = _projects_ctx(),
        topics_ctx    = _topics_ctx(),
        assignment_corrections_ctx = _assignment_corrections_ctx(),
        priority_overrides_ctx     = _priority_overrides_ctx(),
        task_ctx      = _task_ctx(),
        approval_ctx  = _approval_ctx(),
        fyi_ctx       = _fyi_ctx(),
        noise_ctx     = _noise_ctx(),
        to_field     = to_field,
        cc_field     = cc_field,
        sender_hint     = sender_hint,
        replied_hint    = replied_hint,
        manual_tag_hint = manual_tag_hint,
        graph_hint      = graph_hint,
        embedding_hint  = embedding_hint,
        recipient_scope_hint = recipient_scope_hint,
        thread_todos_hint = thread_todos_hint,
    )


def build_analysis_from_llm_json(
    item: RawItem,
    llm_json_text: str,
    *,
    scope_info: dict,
) -> Analysis:
    """
    Parse an LLM JSON response and build a fully populated ``Analysis``.

    Shared by :func:`analyze` (sync path) and the batch poll path in
    :mod:`orchestrator` so the two cannot drift.  Applies every deterministic
    override the sync path applies:

    - ``_detect_quarantine_noise`` (quarantine digests → noise)
    - ``fyi`` / ``noise`` clears any action items the LLM returned
    - Jira fallback (open tickets always get a "Work on: …" action item)
    - ``_detect_passdown`` (deterministic shift handoff detection)
    - ``_validated_project_tags`` (drops invented project names)
    - Both ``project_tags`` (plural) and ``project_tag`` (singular) keys

    The caller is responsible for computing ``scope_info`` from the item's
    To/CC fields and (in the sync path) for surfacing it as a prompt hint.

    Reads from ``item.metadata``:
        ``to``, ``cc``, ``is_replied``, ``replied_at``, ``hierarchy``,
        ``direction``, ``conversation_id``, ``conversation_topic``,
        ``project_tag``, ``due``.

    :param item: The raw item that was analysed.
    :param llm_json_text: Raw LLM response text (will be tolerantly parsed).
    :param scope_info: Result of :func:`compute_recipient_scope`.
    :return: A populated ``Analysis`` instance.
    """
    try:
        data = json.loads(llm_json_text or "{}")
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

    # No regex safety net here by design (issue #83): the LLM is given a
    # cleaned body (quoted chain + SafeLinks stripped) and explicit
    # negative-example rules in the prompt.  Relying on string-match heuristics
    # over a noisy body was the failure mode we just fixed.

    information_items = [
        {"fact": i.get("fact", ""), "relevance": i.get("relevance", "")}
        for i in data.get("information_items", [])
        if i.get("fact")
    ]

    category  = data.get("category", "fyi")
    task_type = data.get("task_type")  # "reply" | "review" | None

    # Deterministic override: quarantine digest with unknown sender → always noise.
    if _detect_quarantine_noise(item):
        category = "noise"

    if category in ("fyi", "noise"):
        action_items = []

    # Jira fallback — always surface open tickets even if the LLM returns sparse
    # output or assigns category=fyi.  Must run after the fyi-clear above.
    if item.source == "jira" and not action_items:
        action_items = [ActionItem(
            description = f"Work on: {item.title}",
            deadline    = item.metadata.get("due"),
            owner       = "me",
        )]

    return Analysis(
        item_id           = item.item_id,
        source            = item.source,
        title             = item.title,
        author            = item.author,
        timestamp         = item.timestamp,
        url               = item.url,
        category          = category,
        task_type         = task_type,
        has_action        = bool(action_items),
        priority          = data.get("priority", "medium"),
        action_items      = action_items,
        summary           = data.get("summary", item.title),
        urgency_reason    = data.get("urgency_reason"),
        hierarchy         = data.get("hierarchy", item.metadata.get("hierarchy", "general")),
        is_passdown       = _detect_passdown(item.title, item.body) or bool(data.get("is_passdown", False)),
        project_tag       = db.serialize_project_tags(
                               _validated_project_tags(
                                   data.get("project_tags")
                                   or data.get("project_tag")
                                   or item.metadata.get("project_tag")
                               )
                           ),
        direction         = item.metadata.get("direction", "received"),
        conversation_id   = item.metadata.get("conversation_id"),
        conversation_topic = item.metadata.get("conversation_topic"),
        goals             = [g for g in data.get("goals", []) if isinstance(g, str) and g],
        key_dates         = [d for d in data.get("key_dates", []) if isinstance(d, dict)],
        body_preview      = _strip_caution(item.body)[:2000],
        to_field          = item.metadata.get("to", ""),
        cc_field          = item.metadata.get("cc", ""),
        is_replied        = bool(item.metadata.get("is_replied", False)),
        replied_at        = item.metadata.get("replied_at"),
        information_items = information_items,
    )


def analyze(
    item: RawItem,
    *,
    priority: str = "short",
    thread_todos: list[dict] | None = None,
) -> Analysis:
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
    :param priority: merLLM priority bucket. Defaults to ``short`` for the
        per-item ingest path; re-analyze passes ``background`` so bulk
        re-runs cannot starve chat or regular ingest traffic.
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
            f"and hierarchy assignment. Include it in project_tags."
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

    # Graph context — related prior items from the knowledge graph
    graph_hint = ""
    try:
        import graph as _graph
        ctx_items = _graph.get_context(item, max_n=4)
        ctx_text  = _graph.format_context(ctx_items)
        if ctx_text:
            graph_hint = f"\n- {ctx_text}"
    except Exception as e:
        log.warning("graph context failed: %s", e)

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
            log.warning("embedding score failed: %s", e)

    scope_info          = compute_recipient_scope(config.USER_EMAIL or "", to_field, cc_field)
    recipient_scope_hint = _recipient_scope_hint(scope_info, _user_name)
    thread_todos_hint    = _render_thread_todos_hint(thread_todos)

    text = llm.generate(
        PROMPT.format(
            source       = item.source,
            title        = item.title,
            author       = item.author,
            timestamp    = item.timestamp,
            body         = _clean_body_for_llm(item.body),
            user_name    = config.USER_NAME or "the user",
            user_email   = config.USER_EMAIL or "",
            projects_ctx  = _projects_ctx(),
            topics_ctx    = _topics_ctx(),
            assignment_corrections_ctx = _assignment_corrections_ctx(),
            priority_overrides_ctx    = _priority_overrides_ctx(),
            task_ctx      = _task_ctx(),
            approval_ctx  = _approval_ctx(),
            fyi_ctx       = _fyi_ctx(),
            noise_ctx     = _noise_ctx(),
            to_field     = to_field,
            cc_field     = cc_field,
            sender_hint     = sender_hint,
            replied_hint    = replied_hint,
            manual_tag_hint = manual_tag_hint,
            graph_hint      = graph_hint,
            embedding_hint  = embedding_hint,
            recipient_scope_hint = recipient_scope_hint,
            thread_todos_hint = thread_todos_hint,
        ),
        format="json", temperature=0.1, num_predict=768, timeout=90,
        priority=priority,
    )

    return build_analysis_from_llm_json(item, text, scope_info=scope_info)


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
            log.error("%s: %s", item.item_id, e)
    return results
