"""Tests for the email signature parser (squire#31).

The parser is a *suggestion engine*: it scores each field's confidence and
``apply_to_contact`` writes only when the score is above threshold and the
field is not in ``manually_edited_fields``.  These tests cover both halves
— the heuristic extraction (``parse_signature`` / ``extract_signature_block``)
and the apply rules (provenance stamping, manual-edit lockout, name fill-only-
when-empty, idempotent re-parse).
"""
import db
import contacts
import signatures


# ── Fixtures: realistic-ish signature corpus ─────────────────────────────────

CLASSIC_WITH_SIGNOFF = """Hi Bob,

Sounds good — see attached.

Thanks,
Jane Doe
Senior Engineer | Acme Corp
123 Main St
Springfield, IL 62701
(555) 123-4567

CONFIDENTIALITY NOTICE: this message is privileged...
"""

RFC_DASHES = """Bob,

Please review the drawings.

--
Mike Jones
Director of Engineering
Globex Industries
+1 (555) 987-6543
"""

QUOTED_REPLY_BELOW_SIG = """Sure, let me look into that.

Best regards,
Sarah Lee
VP Operations
Initech LLC
555.222.3344

On Mon, Apr 1, 2026 at 10:00 AM Bob <bob@x.com> wrote:
> Can you take a look?
> Thanks
> Bob
"""

FORWARDED_THREAD = """Bob,

FYI, see Tom's note below.

Cheers,
Pat Black
Project Manager | Wayne Enterprises
(212) 555-0001

From: Tom Smith
Sent: Mon, Apr 1
Subject: x

Hi all — this is Tom.
"""

GREETING_ONLY = """Sounds good.

Thanks,
John
"""

DISCLAIMER_ONLY = """Approved.

CONFIDENTIALITY NOTICE: This email and any attachments are
confidential and proprietary information.
"""


# ── extract_signature_block ──────────────────────────────────────────────────

def test_extract_block_with_signoff_anchors_after_thanks():
    block = signatures.extract_signature_block(CLASSIC_WITH_SIGNOFF)
    # The block must NOT contain salutation or message body — only the lines
    # after the "Thanks," sign-off.
    assert "Hi Bob" not in block
    assert "Sounds good" not in block
    assert "Jane Doe" in block
    assert "(555) 123-4567" in block
    # Disclaimer footer must be stripped.
    assert "CONFIDENTIALITY" not in block


def test_extract_block_anchors_on_rfc_delimiter():
    block = signatures.extract_signature_block(RFC_DASHES)
    assert "Please review" not in block
    assert "Mike Jones" in block
    assert "Director of Engineering" in block


def test_extract_block_walks_past_quoted_reply_below_signature():
    """The signature lives ABOVE the quoted tail — top-down quote-marker
    cutoff is what makes this case work."""
    block = signatures.extract_signature_block(QUOTED_REPLY_BELOW_SIG)
    assert "Sarah Lee" in block
    assert "555.222.3344" in block
    # None of the quoted lines should leak in.
    assert "Can you take a look" not in block
    assert "> Bob" not in block


def test_extract_block_handles_forwarded_header():
    block = signatures.extract_signature_block(FORWARDED_THREAD)
    assert "Pat Black" in block
    assert "(212) 555-0001" in block
    assert "Tom Smith" not in block      # forwarded header chopped
    assert "this is Tom" not in block


def test_extract_block_strips_pure_disclaimer():
    block = signatures.extract_signature_block(DISCLAIMER_ONLY)
    # All that remains is "Approved." — no disclaimer text.
    assert "CONFIDENTIALITY" not in block
    assert "confidential" not in block.lower()


def test_extract_block_empty_body():
    assert signatures.extract_signature_block("") == ""
    assert signatures.extract_signature_block(None) == ""


# ── parse_signature ──────────────────────────────────────────────────────────

def test_parse_classic_signature_extracts_all_fields():
    block  = signatures.extract_signature_block(CLASSIC_WITH_SIGNOFF)
    fields = signatures.parse_signature(block, sender_domain="acme.com")
    assert fields.name     == "Jane Doe"
    assert fields.phone    == "(555) 123-4567"
    assert fields.title    == "Senior Engineer"
    assert fields.employer == "Acme Corp"
    assert "Springfield" in fields.employer_address
    assert fields.phone_conf >= 0.9
    assert fields.title_conf >= 0.7


def test_parse_rfc_signature_extracts_phone_title_employer():
    block  = signatures.extract_signature_block(RFC_DASHES)
    fields = signatures.parse_signature(block, sender_domain="globex.com")
    assert fields.name     == "Mike Jones"
    assert "555" in fields.phone
    assert "Director" in fields.title
    assert "Globex" in fields.employer
    # No address in this fixture.
    assert fields.employer_address == ""


def test_parse_quoted_reply_extracts_signature_above_quote():
    block  = signatures.extract_signature_block(QUOTED_REPLY_BELOW_SIG)
    fields = signatures.parse_signature(block, sender_domain="initech.com")
    assert fields.name  == "Sarah Lee"
    assert fields.phone == "555.222.3344"


def test_parse_greeting_only_below_threshold():
    """A bare 'Thanks, John' must NOT score high enough to write a name."""
    block  = signatures.extract_signature_block(GREETING_ONLY)
    fields = signatures.parse_signature(block, sender_domain="acme.com")
    # Even if the heuristic picks up "John" as a name candidate, the
    # confidence (0.65 by design) must be strictly below the default
    # threshold so apply_to_contact will refuse to write it.
    assert fields.name_conf < signatures.DEFAULT_CONFIDENCE_THRESHOLD
    # And there's no phone signal at all.
    assert fields.phone == ""


def test_parse_disclaimer_only_yields_no_phone():
    """A pure disclaimer body must produce no phone hit and an empty address."""
    block  = signatures.extract_signature_block(DISCLAIMER_ONLY)
    fields = signatures.parse_signature(block, sender_domain="acme.com")
    assert fields.phone == ""
    assert fields.employer_address == ""


def test_parse_freemail_domain_yields_no_employer_guess():
    fields = signatures.parse_signature("\n".join([
        "Jane Doe", "Engineer", "(555) 123-4567",
    ]), sender_domain="gmail.com")
    # Gmail is in the freemail blocklist — no domain-based employer guess.
    # The third line "(555) 123-4567" is filtered out as a phone, so
    # employer falls back to None and stays empty.
    assert fields.employer == ""


# ── apply_to_contact: provenance + manual-edit guardrails ───────────────────

def _seed_contact_via_header(name, email, item_id="i1"):
    """Helper: create a contact the same way the live ingestion path does."""
    return db.upsert_contact_from_header(name, email, item_id=item_id)


def test_apply_writes_high_confidence_fields_with_signature_provenance():
    cid = _seed_contact_via_header("Jane Doe", "jane@acme.com")
    block  = signatures.extract_signature_block(CLASSIC_WITH_SIGNOFF)
    fields = signatures.parse_signature(block, sender_domain="acme.com")
    result = signatures.apply_to_contact(cid, fields)

    assert result["updated"] is True
    assert "phone" in result["fields"]

    contact = db.get_contact(cid)
    assert contact["phone"]            == "(555) 123-4567"
    assert contact["phone_source"]     == "signature"
    assert contact["title"]            == "Senior Engineer"
    assert contact["title_source"]     == "signature"
    assert contact["employer"]         == "Acme Corp"
    assert contact["employer_source"]  == "signature"
    assert "Springfield" in contact["employer_address"]
    assert contact["address_source"]   == "signature"
    # Header-sourced name must be preserved (signature parser must NOT
    # overwrite a name that came from headers).
    assert contact["name"]        == "Jane Doe"
    assert contact["name_source"] == "header"
    # Confidence map persisted for the UI.
    assert contact["signature_confidence"]["phone"] >= 0.9


def test_apply_skips_below_threshold_fields():
    cid = _seed_contact_via_header("Jane Doe", "jane@acme.com")
    fields = signatures.SignatureFields(
        phone="555-1212", phone_conf=0.5,        # below 0.7
        title="Engineer", title_conf=0.4,
    )
    result = signatures.apply_to_contact(cid, fields)
    assert result["updated"] is False
    contact = db.get_contact(cid)
    assert contact["phone"] == ""
    assert contact["phone_source"] == "header"


def test_apply_respects_manually_edited_fields(client):
    """A field listed in manually_edited_fields must NEVER be overwritten,
    even at 100% confidence."""
    cid = _seed_contact_via_header("Jane Doe", "jane@acme.com")
    # Simulate a user PATCH that locked the phone field.
    r = client.patch(f"/contacts/{cid}", json={"phone": "555-MANUAL"})
    assert r.status_code == 200
    assert r.json()["phone_source"] == "manual"
    assert "phone" in r.json()["manually_edited_fields"]

    # Now run the signature parser at full confidence.
    fields = signatures.SignatureFields(
        phone="(555) 999-9999", phone_conf=1.0,
        title="VP", title_conf=1.0,
    )
    signatures.apply_to_contact(cid, fields)

    contact = db.get_contact(cid)
    assert contact["phone"]        == "555-MANUAL"   # locked
    assert contact["phone_source"] == "manual"
    # Title was not locked, so it should still get written.
    assert contact["title"]        == "VP"
    assert contact["title_source"] == "signature"


def test_apply_fills_empty_name_only_once():
    """Name fill rule: only when the existing name is empty.  Subsequent
    parses must not overwrite it."""
    cid = db.upsert_contact_from_header("", "anon@acme.com")  # empty name
    fields = signatures.SignatureFields(name="Anon Anderson", name_conf=0.65)
    signatures.apply_to_contact(cid, fields)
    assert db.get_contact(cid)["name"] == "Anon Anderson"
    assert db.get_contact(cid)["name_source"] == "signature"

    # A second parse with a different (incorrect) name must not overwrite.
    fields2 = signatures.SignatureFields(name="Wrong Person", name_conf=0.95)
    signatures.apply_to_contact(cid, fields2)
    assert db.get_contact(cid)["name"] == "Anon Anderson"


def test_apply_is_idempotent():
    cid = _seed_contact_via_header("Jane Doe", "jane@acme.com")
    block  = signatures.extract_signature_block(CLASSIC_WITH_SIGNOFF)
    fields = signatures.parse_signature(block, sender_domain="acme.com")

    r1 = signatures.apply_to_contact(cid, fields)
    r2 = signatures.apply_to_contact(cid, fields)

    assert r1["updated"] is True
    # Second pass must be a no-op (no fields changed → no writes).
    assert r2["updated"] is False
    contact = db.get_contact(cid)
    assert contact["phone_source"] == "signature"


# ── parse_item_body: live ingestion glue ─────────────────────────────────────

def _seed_item_with_body(item_id, author, body, to_field=""):
    db.upsert_item({
        "item_id":      item_id,
        "source":       "outlook",
        "author":       author,
        "to_field":     to_field,
        "cc_field":     "",
        "timestamp":    "2026-04-10T12:00:00",
        "title":        "test",
        "summary":      "test",
        "body_preview": body,
    })


def test_parse_item_body_no_op_when_contact_missing():
    """If header scraping has not yet created the contact, parse_item_body
    must do nothing (and return a falsy 'applied')."""
    result = signatures.parse_item_body({
        "item_id":      "x",
        "author":       "Jane <jane@acme.com>",
        "body_preview": CLASSIC_WITH_SIGNOFF,
    })
    assert result["applied"] is False


def test_parse_item_body_enriches_existing_contact():
    cid = db.upsert_contact_from_header("Jane Doe", "jane@acme.com", item_id="i1")
    result = signatures.parse_item_body({
        "item_id":      "i1",
        "author":       "Jane Doe <jane@acme.com>",
        "body_preview": CLASSIC_WITH_SIGNOFF,
    })
    assert result["applied"] is True
    assert result["contact_id"] == cid
    contact = db.get_contact(cid)
    assert contact["phone"] == "(555) 123-4567"


def test_parse_item_body_no_op_when_no_body():
    cid = db.upsert_contact_from_header("Jane Doe", "jane@acme.com", item_id="i1")
    result = signatures.parse_item_body({
        "item_id":      "i1",
        "author":       "Jane Doe <jane@acme.com>",
        "body_preview": "",
    })
    assert result["applied"] is False
    # Contact should be untouched.
    assert db.get_contact(cid)["phone"] == ""


# ── End-to-end via the live save path / endpoints ───────────────────────────

def test_reparse_endpoint_walks_existing_items(client):
    """Seed an item + matching contact, then call /contacts/reparse-signatures
    and verify it enriches the contact row."""
    cid = db.upsert_contact_from_header("Jane Doe", "jane@acme.com", item_id="item-1")
    _seed_item_with_body(
        "item-1",
        author='Jane Doe <jane@acme.com>',
        body=CLASSIC_WITH_SIGNOFF,
    )
    r = client.post("/contacts/reparse-signatures")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["items_scanned"]  >= 1
    assert body["items_applied"]  >= 1
    assert body["fields_written"] >= 1

    contact = db.get_contact(cid)
    assert contact["phone"]        == "(555) 123-4567"
    assert contact["phone_source"] == "signature"


def test_reparse_endpoint_does_not_overwrite_manual_edits(client):
    cid = db.upsert_contact_from_header("Jane Doe", "jane@acme.com", item_id="item-1")
    _seed_item_with_body(
        "item-1",
        author='Jane Doe <jane@acme.com>',
        body=CLASSIC_WITH_SIGNOFF,
    )
    # Lock the phone field via PATCH.
    client.patch(f"/contacts/{cid}", json={"phone": "555-LOCKED"})

    r = client.post("/contacts/reparse-signatures")
    assert r.status_code == 200

    contact = db.get_contact(cid)
    assert contact["phone"]        == "555-LOCKED"
    assert contact["phone_source"] == "manual"
    # Other fields should still be enriched.
    assert contact["title"]        == "Senior Engineer"
    assert contact["title_source"] == "signature"


def test_patch_contact_stamps_manual_provenance(client):
    """Direct test of the PATCH stamping contract: any editable field in
    the request body must flip to source='manual' and join
    manually_edited_fields."""
    r = client.post("/contacts", json={
        "name":  "Jane",
        "emails": ["jane@acme.com"],
    })
    cid = r.json()["contact_id"]
    # Manual create already locked 'name'.
    assert "name" in r.json()["manually_edited_fields"]
    assert r.json()["name_source"] == "manual"

    # Edit phone via PATCH; existing locks must be preserved.
    r2 = client.patch(f"/contacts/{cid}", json={"phone": "555-1212"})
    assert r2.status_code == 200
    locks = set(r2.json()["manually_edited_fields"])
    assert "name"  in locks
    assert "phone" in locks
    assert r2.json()["phone_source"] == "manual"
