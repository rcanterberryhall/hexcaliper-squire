"""
signatures.py — Email-signature parser for the contacts table (squire#31).

The contacts table is auto-populated from email *headers* by ``contacts.py``.
This module is the second pass: it walks the bottom of an email body, isolates
what looks like a signature block, and pulls out phone / title / employer /
address into the matching contact row.

Why this is its own module
--------------------------
Header scraping has structured fields that are always there.  Signature
parsing has none of those guarantees: every sender uses their own format,
quoted reply chains and disclaimers look like signatures to a naive regex,
and bad data poisons the contacts DB.  So we treat the parser as a *suggestion
engine* — it scores its confidence per field, only writes when the score is
above a threshold, and **never** overwrites a field the user has manually
edited (tracked via ``contacts.manually_edited_fields``).

Architecture
------------
1. ``extract_signature_block(body)`` — strip quoted-reply tails and
   disclaimer footers, return the bottom ~15 lines that look signature-like.
2. ``parse_signature(block, sender_domain)`` — regex/heuristic extraction
   producing a ``SignatureFields`` dataclass with per-field confidence.
3. ``apply_to_contact(contact_id, fields, threshold)`` — merge into the
   contacts row, respecting manual edits and stamping ``*_source='signature'``
   on every field actually written.
4. ``parse_item_body(item, threshold)`` — convenience wrapper called from the
   live ingestion path; resolves the contact by author email, then runs the
   pipeline.  Failures are swallowed (parsing must never break ingestion).

This file does *not* parse HTML — ``items.body_preview`` is plain text in
every connector that currently exists.  Full HTML body parsing is a future
extension if/when richer body fields land in the items table.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import db
from agent import extract_emails

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

#: How many trailing lines of the body we even look at.  Real signatures are
#: almost always inside the last 12-15 lines; widening this just invites
#: false positives from the message body.
MAX_TAIL_LINES = 18

#: Default confidence threshold for auto-applying a parsed field.  Phone
#: regex hits land near 0.95, structural name/title guesses around 0.6, so
#: 0.7 keeps the high-signal fields and skips the speculative ones.  The
#: threshold is overridable per call so the future "review queue" UI can
#: surface lower-confidence guesses without writing them.
DEFAULT_CONFIDENCE_THRESHOLD = 0.7

#: Lower threshold used *only* for the empty-name fill path.  The parser
#: deliberately scores names at ~0.65 (below the main threshold) so it
#: cannot overwrite a header-sourced name, but the fill-when-empty path
#: still needs to be reachable.  The "name must currently be empty"
#: precondition is the load-bearing guardrail here, not the threshold.
NAME_FILL_THRESHOLD = 0.6

#: Lines that mark the start of a quoted reply tail.  Anything from one of
#: these markers downward is *not* a signature, no matter what comes after.
#: Order matters: the first match wins, so put the most specific ones first.
_QUOTE_MARKERS = (
    re.compile(r"^\s*>"),                                       # > quoted text
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.I),  # Outlook
    re.compile(r"^\s*From:\s.+", re.I),                         # forwarded header
    re.compile(r"^\s*On\s.+\swrote:\s*$", re.I),                # On <date> ... wrote:
    re.compile(r"^\s*Sent from my\s", re.I),                    # mobile sigs we don't want
    re.compile(r"^\s*Get Outlook for", re.I),
)

#: Disclaimer detector.  Anything from a disclaimer line downward is also
#: dropped — these are usually appended after the actual signature block, but
#: when they appear *inside* the tail window we want to chop them out.
_DISCLAIMER_MARKERS = (
    re.compile(r"confidentiality\s+notice", re.I),
    re.compile(r"this\s+(e[- ]?mail|message)\s+(is|and any)\s+(confidential|intended)", re.I),
    re.compile(r"privileged\s+(and|or)\s+confidential", re.I),
    re.compile(r"please\s+consider\s+the\s+environment", re.I),
    re.compile(r"do\s+not\s+disclose", re.I),
)

#: Common explicit signature delimiters.  Standard "-- " (dash dash space) is
#: the RFC convention; "--" without the trailing space and underscore lines
#: are very common in the wild too.
_SIG_DELIMITERS = (
    re.compile(r"^\s*--\s*$"),
    re.compile(r"^\s*-{2,}\s*$"),
    re.compile(r"^\s*_{3,}\s*$"),
    re.compile(r"^\s*={3,}\s*$"),
)

#: Phone-number regex.  Tolerates +country, parens, dashes, dots, spaces, and
#: optional extension.  Demands at least 10 digits to keep "v1.2.3" out.
_PHONE_RE = re.compile(
    r"""
    (?:                              # optional leading +country code
        \+?\d{1,3}[\s.\-]?
    )?
    \(?\d{3}\)?[\s.\-]?              # area code, with or without parens
    \d{3}[\s.\-]?                    # exchange
    \d{4}                            # subscriber
    (?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?   # optional extension
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: ZIP / postal-style address tail.  Used to score the confidence of an
#: "address" line — we never try to *normalise* the address, just decide
#: whether the multi-line block at the bottom looks address-shaped.
_US_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_STATE_ABBR_RE = re.compile(r"\b[A-Z]{2}\b")

#: A name line is at most 4 words, mostly title-case, no digits, no @, and no
#: punctuation other than ., ', -.  This is intentionally loose — most actual
#: name lines pass it, and the false positives we care about (greetings like
#: "Thanks, John") are filtered separately.
_NAME_LINE_RE = re.compile(
    r"^[A-Z][A-Za-z'\-\.]+(?:\s+[A-Z][A-Za-z'\-\.]+){0,3}$"
)

#: Greeting words that often immediately precede a name on a single line and
#: would otherwise be parsed *as* a name.  Treated as evidence the line is a
#: sign-off, not a signature start.
_GREETING_WORDS = {
    "thanks", "thank", "thx", "regards", "best", "cheers", "sincerely",
    "br", "kind", "kindly", "respectfully", "cordially",
}

#: Sign-off lines that imply "the signature starts on the next line."  Used
#: as a fallback anchor in ``extract_signature_block`` when there is no
#: explicit ``-- `` delimiter.  Match is case-insensitive and tolerates
#: optional trailing punctuation/extra word ("Best regards,", "Thanks!").
_SIGNOFF_RE = re.compile(
    r"^\s*(?:" + "|".join([
        r"thanks(?:\s+again)?",
        r"thank\s+you",
        r"thx",
        r"regards",
        r"best(?:\s+regards)?",
        r"kind(?:\s+regards)?",
        r"cheers",
        r"sincerely",
        r"respectfully",
        r"cordially",
        r"yours(?:\s+truly)?",
    ]) + r")[,!.\s]*$",
    re.IGNORECASE,
)

#: Title keywords — used to bump confidence when a candidate "title" line
#: contains one.  Not exhaustive; only high-signal terms.
_TITLE_KEYWORDS = {
    "engineer", "manager", "director", "president", "ceo", "cto", "cfo",
    "coo", "vp", "vice", "lead", "principal", "senior", "sr", "jr",
    "consultant", "specialist", "analyst", "architect", "developer",
    "administrator", "coordinator", "supervisor", "owner", "founder",
    "partner", "associate", "officer", "head", "chief",
}


# ── Public API ───────────────────────────────────────────────────────────────

@dataclass
class SignatureFields:
    """Result of a single signature parse, with per-field confidence (0..1).

    Empty strings mean "no signal at all" — distinct from a low-confidence
    guess.  ``apply_to_contact`` will skip writing any field whose confidence
    is below its threshold, so a guess that comes back as
    ``("Acme", 0.4)`` is still recorded for diagnostic purposes but will not
    actually update the row.
    """
    name:             str   = ""
    name_conf:        float = 0.0
    phone:            str   = ""
    phone_conf:       float = 0.0
    title:            str   = ""
    title_conf:       float = 0.0
    employer:         str   = ""
    employer_conf:    float = 0.0
    employer_address: str   = ""
    address_conf:     float = 0.0

    def is_empty(self) -> bool:
        """True if the parser found nothing usable at all."""
        return not any((
            self.name, self.phone, self.title,
            self.employer, self.employer_address,
        ))

    def confidence_map(self) -> dict[str, float]:
        """Confidence dict suitable for storage in ``signature_confidence``.

        Only includes fields the parser actually populated; an empty value
        with a 0.0 score is omitted so the persisted JSON stays small.
        """
        out: dict[str, float] = {}
        if self.name:             out["name"]             = round(self.name_conf, 2)
        if self.phone:            out["phone"]            = round(self.phone_conf, 2)
        if self.title:            out["title"]            = round(self.title_conf, 2)
        if self.employer:         out["employer"]         = round(self.employer_conf, 2)
        if self.employer_address: out["employer_address"] = round(self.address_conf, 2)
        return out


def extract_signature_block(body: str) -> str:
    """Return the trailing chunk of *body* that looks like a signature.

    The walk is bottom-up so a quoted reply tail or a disclaimer that lives
    *above* the actual signature still gets chopped off — we only return what
    sits between the last quote/disclaimer marker and the end of the message.

    The result is at most ``MAX_TAIL_LINES`` lines, leading/trailing whitespace
    stripped.  Returns an empty string if there is nothing left after pruning.
    """
    if not body:
        return ""

    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # Quoted reply tails almost always sit *below* the signature ("On <date>
    # X wrote:" / ">" lines).  Find the first quote marker top-down and chop
    # everything from there to the end of the message — what remains is the
    # author's own text, with the signature at its bottom.
    cutoff = len(lines)
    for idx, raw in enumerate(lines):
        if any(p.search(raw) for p in _QUOTE_MARKERS):
            cutoff = idx
            break
    lines = lines[:cutoff]

    # Walk from the bottom up to MAX_TAIL_LINES.  Disclaimers found in the
    # tail window cause us to drop everything collected so far (the
    # disclaimer body) and keep walking up — the actual signature might
    # live *above* the disclaimer.
    tail: list[str] = []
    for raw in reversed(lines):
        line = raw.rstrip()
        if any(p.search(line) for p in _DISCLAIMER_MARKERS):
            tail = []
            continue
        tail.append(line)
        if len(tail) >= MAX_TAIL_LINES:
            break

    tail.reverse()

    # Trim outer blank lines so the parser starts on real content.
    while tail and not tail[0].strip():
        tail.pop(0)
    while tail and not tail[-1].strip():
        tail.pop()

    if not tail:
        return ""

    # Anchor the signature on whatever comes first:
    #   1. an explicit "-- " (or similar) delimiter line — RFC convention
    #   2. a sign-off line ("Thanks,", "Regards," etc) — common in the wild
    # Once anchored, only the lines *after* the anchor count as the signature.
    # If neither anchor is present we fall back to "the whole tail" — which
    # is fine for short messages that *are* basically a signature, and the
    # downstream parser is conservative about what it writes.
    anchor: Optional[int] = None
    for idx, line in enumerate(tail):
        if any(p.match(line) for p in _SIG_DELIMITERS):
            anchor = idx
            break
    if anchor is None:
        for idx, line in enumerate(tail):
            if _SIGNOFF_RE.match(line):
                anchor = idx
                break
    if anchor is not None:
        tail = tail[anchor + 1:]

    return "\n".join(tail).strip()


def parse_signature(block: str, sender_domain: Optional[str] = None) -> SignatureFields:
    """Heuristic field extraction from a signature block.

    The strategy is intentionally cheap: regex for the high-signal field
    (phone), structural guesses for name/title/employer, and a multi-line
    pattern for the address tail.  Each field gets a confidence score that
    callers can threshold against.

    ``sender_domain`` is the domain part of the sender email — when supplied
    it nudges the employer guess (e.g. ``acme.com`` → ``Acme``) when the body
    has no obvious "Company Name" line.
    """
    out = SignatureFields()
    if not block:
        return out

    lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
    if not lines:
        return out

    # ── Phone (highest-signal field) ─────────────────────────────────────
    # First match wins; phone regex is strict enough that we trust any hit.
    for line in lines:
        m = _PHONE_RE.search(line)
        if m:
            phone = m.group(0).strip()
            digits = re.sub(r"\D", "", phone)
            if 10 <= len(digits) <= 15:
                out.phone      = phone
                out.phone_conf = 0.95
                break

    # ── Address (multi-line ZIP-anchored block) ──────────────────────────
    # Walk bottom-up looking for a US-ZIP line; if found, the previous 1-3
    # *address-shaped* lines plus the ZIP line are the address block.  The
    # walk stops at the first line that obviously isn't an address: a "|"
    # separator (title|employer is the canonical pattern), a phone number,
    # or a known job-title keyword.  This keeps "Senior Engineer | Acme
    # Corp" out of the address field.
    for idx in range(len(lines) - 1, -1, -1):
        if _US_ZIP_RE.search(lines[idx]) and _STATE_ABBR_RE.search(lines[idx]):
            block_lines: list[str] = [lines[idx]]
            for back in range(idx - 1, max(-1, idx - 4), -1):
                line = lines[back]
                if (
                    "|" in line
                    or _PHONE_RE.search(line)
                    or _has_title_keyword(line)
                ):
                    break
                block_lines.insert(0, line)
            out.employer_address = ", ".join(block_lines)
            out.address_conf     = 0.85
            break

    # ── Name + title + employer (structural top-of-block guess) ──────────
    # The classic order is:
    #   Line 1: Full Name
    #   Line 2: Title
    #   Line 3: Employer (or Title | Employer on one line)
    # We trust line 1 only if it parses as a person-name and is not a
    # greeting (`Thanks,` etc).  When line 1 fails, we don't try harder —
    # better to leave the field blank than poison the contacts row.
    candidate_lines = [
        ln for ln in lines
        if not _PHONE_RE.search(ln)                # phone-only lines
        and not _looks_like_email_or_url(ln)
        and not _US_ZIP_RE.search(ln)              # address tail
    ]

    if candidate_lines:
        first = candidate_lines[0]
        first_clean = first.rstrip(",.;:")
        if (
            _NAME_LINE_RE.match(first_clean)
            and first_clean.split()[0].lower() not in _GREETING_WORDS
        ):
            out.name      = first_clean
            out.name_conf = 0.65

            # Title: second non-phone, non-address line if it looks title-y.
            if len(candidate_lines) >= 2:
                second = candidate_lines[1]
                # Title | Company on one line is common — split on |.
                if "|" in second:
                    parts = [p.strip() for p in second.split("|") if p.strip()]
                    if parts:
                        out.title      = parts[0]
                        out.title_conf = 0.7 if _has_title_keyword(parts[0]) else 0.55
                    if len(parts) >= 2:
                        out.employer      = parts[1]
                        out.employer_conf = 0.7
                else:
                    # Bump confidence when the line contains a known title word.
                    out.title      = second
                    out.title_conf = 0.75 if _has_title_keyword(second) else 0.5

            # Employer: third line if we don't already have one.
            if not out.employer and len(candidate_lines) >= 3:
                third = candidate_lines[2]
                if not _has_title_keyword(third):
                    out.employer      = third
                    out.employer_conf = 0.6

    # Domain fallback for employer when we still have nothing.
    if not out.employer and sender_domain:
        guess = _employer_from_domain(sender_domain)
        if guess:
            out.employer      = guess
            # Lower confidence — domain → name is a guess, especially for
            # generic providers like gmail.com (which we filter below).
            out.employer_conf = 0.4

    return out


def apply_to_contact(
    contact_id: int,
    fields: SignatureFields,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> dict:
    """Merge a parsed signature into the contacts row.

    Rules
    -----
    * Never overwrite a field that appears in ``manually_edited_fields``.
    * Never overwrite the ``name`` field — header scraping owns names, the
      signature parser only fills it in when the existing name is empty.
      (Confirmed approach in the squire#31 design discussion.)
    * Skip any field whose confidence is below ``threshold``.
    * Stamp ``<field>_source = 'signature'`` on every field actually written.
    * Always refresh ``signature_confidence`` so the UI sees the latest
      per-field scores even when nothing was written this round.

    Returns a small dict describing what changed (used by the rebuild
    endpoint and tests).
    """
    contact = db.get_contact(contact_id)
    if not contact:
        return {"updated": False, "reason": "contact_not_found"}

    locked: set[str] = set(contact.get("manually_edited_fields") or [])
    updates: dict = {}
    written: list[str] = []

    def _try(field_name: str, value: str, conf: float, source_col: str):
        if not value or conf < threshold:
            return
        if field_name in locked:
            return
        # Skip if the existing value already matches — saves a needless
        # provenance flip.
        if (contact.get(field_name) or "").strip() == value.strip():
            return
        updates[field_name] = value
        updates[source_col] = "signature"
        written.append(field_name)

    # Name is special: only fill if currently empty.  Uses a lower
    # threshold (NAME_FILL_THRESHOLD) than other fields because the
    # "must be empty" precondition is the real safety net here — names
    # are scored at ~0.65 by the parser so they can't ever overwrite a
    # header-sourced name, but the empty-fill path still needs to fire.
    if (
        fields.name
        and fields.name_conf >= NAME_FILL_THRESHOLD
        and "name" not in locked
        and not (contact.get("name") or "").strip()
    ):
        updates["name"]        = fields.name
        updates["name_source"] = "signature"
        written.append("name")

    _try("phone",            fields.phone,            fields.phone_conf,    "phone_source")
    _try("title",            fields.title,            fields.title_conf,    "title_source")
    _try("employer",         fields.employer,         fields.employer_conf, "employer_source")
    _try("employer_address", fields.employer_address, fields.address_conf,  "address_source")

    # Always persist the latest confidence map (even if nothing was written
    # — the UI uses this to show "tried but below threshold").  Merge with
    # existing so older field scores survive when the latest run had no
    # signal for them.
    existing_conf = contact.get("signature_confidence") or {}
    if isinstance(existing_conf, str):
        try:
            existing_conf = json.loads(existing_conf)
        except (json.JSONDecodeError, TypeError):
            existing_conf = {}
    new_conf = fields.confidence_map()
    if new_conf:
        merged = dict(existing_conf)
        merged.update(new_conf)
        updates["signature_confidence"] = merged

    if updates:
        db.update_contact(contact_id, updates)
    return {
        "updated":  bool(written),
        "fields":   written,
        "contact_id": contact_id,
    }


def parse_item_body(item: dict, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> dict:
    """Glue called from the live ingestion path.

    Resolves the *author* of an item to a contact (header scraping must have
    run first), runs the parser on its body, and applies the result.  This
    is intentionally a no-op when:

    * the item has no body
    * the author email is unknown
    * the author email does not yet correspond to a contact
      (``scrape_item_headers`` should have created one moments earlier — if
      it did not, there is no row to enrich and we silently skip)

    All exceptions are caught and logged.  Signature parsing must never
    break the analysis save path.

    Returns ``{"applied": bool, "fields": [...]}`` for tests / API callers.
    """
    if not item:
        return {"applied": False, "reason": "no_item"}
    body = item.get("body_preview") or ""
    if not body.strip():
        return {"applied": False, "reason": "no_body"}

    author_field = item.get("author") or ""
    author_emails = extract_emails(author_field)
    if not author_emails:
        return {"applied": False, "reason": "no_author_email"}
    author_email = author_emails[0].lower()

    try:
        contact = db.get_contact_by_email(author_email)
    except Exception as exc:                                # pragma: no cover
        log.warning("signatures: contact lookup failed for %s: %s", author_email, exc)
        return {"applied": False, "reason": "lookup_error"}
    if not contact:
        return {"applied": False, "reason": "no_contact"}

    sender_domain = author_email.split("@", 1)[1] if "@" in author_email else None

    try:
        block = extract_signature_block(body)
        fields = parse_signature(block, sender_domain=sender_domain)
        if fields.is_empty():
            return {"applied": False, "reason": "no_signal"}
        result = apply_to_contact(
            contact["contact_id"], fields, threshold=threshold
        )
        return {
            "applied": result.get("updated", False),
            "fields":  result.get("fields", []),
            "contact_id": contact["contact_id"],
        }
    except Exception as exc:                                # pragma: no cover
        log.warning("signatures: parse failed for item %s: %s",
                    item.get("item_id"), exc)
        return {"applied": False, "reason": "parse_error"}


def reparse_all_items(threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> dict:
    """Walk every item in the corpus and re-run signature parsing.

    Mirrors ``contacts.rebuild_from_items`` for the body-parsing pass.
    Idempotent: re-running on the same corpus will not flip provenance away
    from manual or change a field whose confidence is unchanged.
    """
    items_scanned = 0
    items_applied = 0
    fields_written = 0
    for item in db.get_all_items():
        items_scanned += 1
        result = parse_item_body(item, threshold=threshold)
        if result.get("applied"):
            items_applied += 1
            fields_written += len(result.get("fields") or [])
    log.info(
        "signatures: rebuild scanned %d items, enriched %d contacts, wrote %d fields",
        items_scanned, items_applied, fields_written,
    )
    return {
        "items_scanned": items_scanned,
        "items_applied": items_applied,
        "fields_written": fields_written,
    }


# ── Internal helpers ─────────────────────────────────────────────────────────

def _looks_like_email_or_url(line: str) -> bool:
    """True if a line is just a URL or an email address (poor name candidate)."""
    if "@" in line and re.search(r"\S+@\S+\.\S+", line):
        return True
    if re.match(r"^\s*(https?://|www\.)", line, re.I):
        return True
    return False


def _has_title_keyword(line: str) -> bool:
    """True if any word in *line* matches the curated title keyword set."""
    words = re.findall(r"[A-Za-z]+", line.lower())
    return any(w in _TITLE_KEYWORDS for w in words)


#: Free-mail providers we should never use as an employer guess.  These domains
#: tell us nothing about who someone works for.
_FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "aol.com", "live.com", "me.com", "msn.com", "protonmail.com",
    "proton.me", "fastmail.com", "gmx.com", "yandex.com", "mail.com",
}


def _employer_from_domain(domain: str) -> Optional[str]:
    """Best-effort 'turn acme.com into Acme' fallback for the employer field.

    Returns None for free-mail providers (gmail.com etc) where the domain
    contains zero employer signal.
    """
    if not domain:
        return None
    domain = domain.lower().strip()
    if domain in _FREEMAIL_DOMAINS:
        return None
    head = domain.split(".")[0]
    if not head or len(head) < 2:
        return None
    # Title-case the head word ("acme" → "Acme", "ge-aviation" → "Ge-Aviation").
    return head.replace("-", " ").title()
