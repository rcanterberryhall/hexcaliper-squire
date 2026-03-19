"""
correlator.py — Cross-source situation correlation and synthesis.

Owns all situation formation logic:
  extract_references()         — pull Jira keys, PR numbers, issue refs from text
  find_correlated_candidates() — deterministic ref match + semantic vector search
  score_situation()            — composite urgency score for a candidate cluster
  synthesize_situation()       — LLM narrative synthesis across items
"""
import json
import math
import re
from datetime import datetime, timezone

import requests

import config

# ── Reference extraction ───────────────────────────────────────────────────────

_REF_PATTERNS = [
    re.compile(r'\b([A-Z][A-Z0-9]+-\d+)\b'),           # Jira keys: PROJ-142
    re.compile(r'\bPR[- ]?#?(\d+)\b', re.IGNORECASE),  # PR numbers
    re.compile(r'\bissue[- ]?#?(\d+)\b', re.IGNORECASE),
    re.compile(r'#(\d{3,6})\b'),                        # bare #NNN
]


def extract_references(title: str, body: str) -> list:
    """
    Extract explicit cross-source identifiers from item title and body.
    Returns a deduplicated lowercase list, e.g. ["proj-142", "pr-89"].
    """
    text = (title or "") + " " + (body or "")
    found = set()
    for i, pat in enumerate(re.finditer(_REF_PATTERNS[0], text)):
        found.add(pat.group(1).lower())
    for pat in _REF_PATTERNS[0].finditer(text):
        found.add(pat.group(1).lower())
    for pat in _REF_PATTERNS[1].finditer(text):
        found.add(f"pr-{pat.group(1)}")
    for pat in _REF_PATTERNS[2].finditer(text):
        found.add(f"issue-{pat.group(1)}")
    for pat in _REF_PATTERNS[3].finditer(text):
        found.add(f"#{pat.group(1)}")
    return sorted(found)


# ── Candidate generation ───────────────────────────────────────────────────────

def find_correlated_candidates(
    item_id: str,
    references: list,
    vector: list,
    project_tag,
    all_analyses: list,
    similarity_threshold: float = 0.82,
) -> list:
    """
    Return item_ids of analyses likely describing the same situation.

    Two-pass approach:
    1. Deterministic: all analyses sharing at least one reference string.
    2. Semantic: analyses whose stored vector has cosine similarity
       >= similarity_threshold to this item's vector, within the same
       project_tag (or both untagged).

    ``all_analyses`` must be provided by the caller (pre-fetched under db_lock)
    to avoid opening a second TinyDB instance concurrently with the main app.

    Returns deduplicated list of item_ids excluding the query item itself.
    """
    candidates = set()

    # Pass 1: deterministic reference matching
    if references:
        ref_set = set(r.lower() for r in references)
        for rec in all_analyses:
            if rec.get("item_id") == item_id:
                continue
            raw = rec.get("references")
            if not raw:
                continue
            try:
                stored = set(json.loads(raw) if isinstance(raw, str) else raw)
            except Exception:
                continue
            if stored & ref_set:
                candidates.add(rec["item_id"])

    # Pass 2: semantic similarity
    if vector:
        try:
            import numpy as np
            from embedder import get_item_vector
            v = np.array(vector)
            for rec in all_analyses:
                cid = rec.get("item_id")
                if not cid or cid == item_id:
                    continue
                # Only correlate within same project or both untagged
                rec_proj = rec.get("project_tag")
                if rec_proj != project_tag:
                    continue
                stored_vec = get_item_vector(cid)
                if stored_vec is None:
                    continue
                score = float(np.dot(v, np.array(stored_vec)))
                if score >= similarity_threshold:
                    candidates.add(cid)
        except Exception as e:
            print(f"[correlator] semantic pass failed: {e}")

    return list(candidates)


# ── Situation scoring ──────────────────────────────────────────────────────────

def score_situation(item_ids: list, analyses: list) -> float:
    """
    Compute a composite urgency score for a candidate situation cluster.

    Components:
      source_score    (0.35) — log2(unique_source_types + 1)
      recency_score   (0.25) — 1 / (1 + hours_since_latest / 12)
      priority_score  (0.25) — max individual priority, normalized 0-1
      addressal_score (0.15) — proportion of user-hierarchy items, amplified

    Returns float in roughly 0.0–2.5 range.
    """
    pri_map = {"high": 3, "medium": 2, "low": 1}

    unique_sources = len(set(a["source"] for a in analyses))
    source_score   = math.log2(unique_sources + 1)

    timestamps = [a.get("timestamp", "") for a in analyses if a.get("timestamp")]
    if timestamps:
        latest = max(timestamps)
        try:
            dt        = datetime.fromisoformat(latest.replace("Z", "+00:00"))
            hours_ago = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            hours_ago = 48
    else:
        hours_ago = 48
    recency_score = 1 / (1 + hours_ago / 12)

    max_pri        = max((pri_map.get(a.get("priority", "low"), 1) for a in analyses), default=1)
    priority_score = max_pri / 3

    user_items      = sum(1 for a in analyses if a.get("hierarchy") == "user")
    addressal_score = min(1.0, user_items / max(len(analyses), 1) + 0.3 * bool(user_items))

    return round(
        source_score    * 0.35 +
        recency_score   * 0.25 +
        priority_score  * 0.25 +
        addressal_score * 0.15,
        3
    )


# ── LLM situation synthesis ────────────────────────────────────────────────────

SYNTHESIS_PROMPT = """You are a personal ops assistant for {user_name}.

The following items from different sources all appear to be about the same situation.
Synthesize them into a single coherent picture.

{items_block}
{intel_block}

Respond ONLY with valid JSON. No explanation, no markdown fences.

{{
  "title": "short descriptive title for this situation (10 words max)",
  "summary": "2-3 sentences: what is happening, current status, what is needed",
  "status": "blocked" or "in_progress" or "waiting" or "needs_decision" or "informational",
  "open_actions": [
    {{
      "description": "specific action required",
      "owner": "me or person name",
      "deadline": "ISO date or null",
      "source_item_id": "item_id this came from"
    }}
  ],
  "key_context": "one sentence of essential background, or null"
}}
"""


def synthesize_situation(item_records: list, user_name: str, intel_items: list = None) -> dict:
    """
    Call Ollama to produce a cross-source narrative for a situation cluster.
    Falls back to a minimal dict on failure.

    items_block: per-item summary lines capped at 6 items × 200 chars each.
    intel_items: optional list of information_items dicts from the intel table.
    """
    capped = item_records[:6]
    lines = []
    for r in capped:
        line = f"[{r.get('source','')}] {r.get('title','')}: {r.get('summary','')[:200]} ({r.get('priority','')}, {r.get('category','')})"
        lines.append(line)
    items_block = "\n".join(lines)

    if intel_items:
        intel_lines = [
            f"- [{i.get('source','')}] {i.get('fact','')} ({i.get('relevance','')[:100]})"
            for i in intel_items[:8]
        ]
        intel_block = "Recent status updates and context:\n" + "\n".join(intel_lines) + "\n"
    else:
        intel_block = ""

    fallback = {
        "title":        _fallback_title(item_records),
        "summary":      "Multiple related items detected across sources.",
        "status":       "in_progress",
        "open_actions": [],
        "key_context":  None,
    }

    if not config.OLLAMA_URL:
        return fallback

    try:
        response = requests.post(
            config.OLLAMA_URL,
            headers=config.ollama_headers(),
            json={
                "model":   config.OLLAMA_MODEL,
                "prompt":  SYNTHESIS_PROMPT.format(
                    user_name   = user_name or "the user",
                    items_block = items_block,
                    intel_block = intel_block,
                ),
                "stream":  False,
                "format":  "json",
                "options": {"temperature": 0.1, "num_predict": 512},
            },
            timeout=60,
        )
        response.raise_for_status()
        data = json.loads(response.json().get("response", "{}"))
        return {
            "title":        data.get("title")        or fallback["title"],
            "summary":      data.get("summary")      or fallback["summary"],
            "status":       data.get("status")       or "in_progress",
            "open_actions": data.get("open_actions") or [],
            "key_context":  data.get("key_context"),
        }
    except Exception as e:
        print(f"[correlator] synthesis failed: {e}")
        return fallback


def _fallback_title(records: list) -> str:
    sources = list(dict.fromkeys(r.get("source", "") for r in records))
    if records:
        return records[0].get("title", "Correlated situation")[:60]
    return "Correlated situation"
