"""
contacts.py — Contacts table population from email headers.

The contacts table is identified by a stable serial integer (`contact_id`),
**never** by email — emails change when people switch jobs, but the person
record should outlive the address.  This module owns:

  * Parsing ``"Display Name <email@host>"`` pairs from raw header strings.
  * Upserting them into the contacts + contact_emails tables.
  * Scraping a single item's headers (live, from the analysis path).
  * Rebuilding the entire contacts table from the existing items corpus.

Signature parsing (extracting phone/title from email body footers) is a
separate, larger effort and lives in its own follow-up issue — do not bundle
it here.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

import db
from agent import _NAME_EMAIL_RE, extract_emails

log = logging.getLogger(__name__)


def parse_header_pairs(field: str) -> list[tuple[str, str]]:
    """Pull ``(display_name, email)`` tuples from a raw header string.

    Picks up RFC-style pairs like ``"Jane Doe <jane@acme.com>"`` first, then
    falls back to bare addresses (no display name) for the rest.  Emails are
    lowercased; display names are stripped of surrounding whitespace and
    quotes.
    """
    if not field:
        return []
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    # 1. RFC-style "Name <addr>" pairs
    for match in _NAME_EMAIL_RE.finditer(field):
        name  = match.group(1).strip().strip('"').strip("'")
        email = match.group(2).lower()
        if email in seen:
            continue
        seen.add(email)
        pairs.append((name, email))

    # 2. Bare addresses that weren't part of a Name <addr> pair
    for email in extract_emails(field):
        if email in seen:
            continue
        seen.add(email)
        pairs.append(("", email))

    return pairs


def scrape_item_headers(item: dict) -> int:
    """Upsert every contact found in an item's author/to/cc fields.

    Designed to be called from the analysis save path.  Failures are logged
    but never raised — contact scraping must not break ingestion.

    Returns the number of contact rows touched (created or bumped).
    """
    if not item:
        return 0
    item_id   = item.get("item_id")
    timestamp = item.get("timestamp")

    fields = (
        item.get("author")   or "",
        item.get("to_field") or "",
        item.get("cc_field") or "",
    )

    # NOTE: this helper does NOT acquire ``db.lock`` itself.  The live caller
    # in app._save_analysis already holds it (the whole save block is wrapped
    # in ``with db.lock:``), and a re-entry on a non-reentrant
    # ``threading.Lock`` would self-deadlock.  Single SQLite statements are
    # already atomic, so the rebuild caller doesn't need to wrap either.
    touched = 0
    for field in fields:
        for display_name, email in parse_header_pairs(field):
            try:
                db.upsert_contact_from_header(
                    display_name=display_name,
                    email=email,
                    item_id=item_id,
                    item_timestamp=timestamp,
                )
                touched += 1
            except Exception as exc:                       # pragma: no cover
                log.warning("contacts: failed to upsert %s: %s", email, exc)
    return touched


def rebuild_from_items(items: Optional[Iterable[dict]] = None) -> dict:
    """Walk every item (or a provided subset) and populate contacts.

    Idempotent — re-running on the same corpus will bump source counts but
    will not duplicate rows because emails are unique in contact_emails.

    Returns a summary dict with ``items_scanned`` and ``contacts_touched``.
    """
    if items is None:
        items = db.get_all_items()

    items_scanned    = 0
    contacts_touched = 0
    for item in items:
        items_scanned   += 1
        contacts_touched += scrape_item_headers(item)

    log.info(
        "contacts: rebuild scanned %d items, touched %d header entries",
        items_scanned, contacts_touched,
    )
    return {
        "items_scanned":    items_scanned,
        "contacts_touched": contacts_touched,
        "total_contacts":   db.count_contacts(),
    }
