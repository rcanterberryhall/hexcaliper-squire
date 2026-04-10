"""Tests for the contacts feature (squire#24).

Covers:
  * parse_header_pairs — RFC-style and bare-address parsing
  * upsert_contact_from_header — merge-by-email semantics
  * scrape_item_headers + rebuild_from_items — backfill correctness
  * resolve_owner_email — fallback into the contacts table when the owner
    name is not present in the current item's To/CC headers
  * Manual CRUD endpoints — full create / read / update / delete + email mgmt
"""
import agent
import contacts
import db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_item(client, item_id, author, to_field="", cc_field=""):
    """Insert a minimal item row directly via db so we don't need the LLM."""
    db.upsert_item({
        "item_id":   item_id,
        "source":    "outlook",
        "author":    author,
        "to_field":  to_field,
        "cc_field":  cc_field,
        "timestamp": "2026-04-10T12:00:00",
        "title":     "test",
        "summary":   "test",
    })


# ── Header parsing ────────────────────────────────────────────────────────────

def test_parse_header_pairs_rfc_style():
    pairs = contacts.parse_header_pairs(
        '"Jane Doe" <jane@acme.com>, Bob <bob@acme.com>'
    )
    assert pairs == [("Jane Doe", "jane@acme.com"), ("Bob", "bob@acme.com")]


def test_parse_header_pairs_falls_back_to_bare_addresses():
    pairs = contacts.parse_header_pairs(
        'Jane <jane@acme.com>, raw@acme.com'
    )
    assert ("Jane", "jane@acme.com") in pairs
    assert ("", "raw@acme.com")      in pairs
    # No duplicates even if the same email appears twice
    pairs2 = contacts.parse_header_pairs("a@x.com, a@x.com")
    assert len(pairs2) == 1


def test_parse_header_pairs_handles_empty():
    assert contacts.parse_header_pairs("") == []
    assert contacts.parse_header_pairs(None) == []


# ── Upsert / merge semantics ─────────────────────────────────────────────────

def test_upsert_contact_from_header_creates_new_contact():
    cid = db.upsert_contact_from_header("Jane Doe", "jane@acme.com", item_id="i1")
    contact = db.get_contact(cid)
    assert contact["name"] == "Jane Doe"
    assert contact["emails"] == ["jane@acme.com"]
    assert contact["primary_email"] == "jane@acme.com"
    assert contact["source_count"] == 1


def test_upsert_contact_from_header_bumps_existing():
    cid1 = db.upsert_contact_from_header("Jane Doe", "jane@acme.com", item_id="i1")
    cid2 = db.upsert_contact_from_header("Jane Doe", "jane@acme.com", item_id="i2")
    assert cid1 == cid2
    contact = db.get_contact(cid1)
    assert contact["source_count"] == 2
    assert contact["last_item_id"] == "i2"


def test_upsert_contact_from_header_fills_missing_name():
    # Seen first as a bare address, then later with a display name.
    cid = db.upsert_contact_from_header("", "jane@acme.com", item_id="i1")
    db.upsert_contact_from_header("Jane Doe", "jane@acme.com", item_id="i2")
    assert db.get_contact(cid)["name"] == "Jane Doe"


def test_email_pk_constraint_does_not_apply():
    """A person's identity must outlive any single email — confirmed by
    creating two contacts manually and verifying they coexist with separate
    contact_ids even when one later acquires the other's old address.
    """
    cid_a = db.insert_contact({"name": "Jane Doe", "emails": ["jane@old.com"]})
    cid_b = db.insert_contact({"name": "Jane Doe (new)", "emails": ["jane@new.com"]})
    assert cid_a != cid_b
    # Stable IDs survive — neither is "the email row".
    assert db.get_contact(cid_a)["contact_id"] == cid_a
    assert db.get_contact(cid_b)["contact_id"] == cid_b


# ── Live scrape + rebuild ─────────────────────────────────────────────────────

def test_scrape_item_headers_extracts_all_fields(client):
    _seed_item(
        client, "item-1",
        author='"Alice Smith" <alice@acme.com>',
        to_field='"Bob Jones" <bob@acme.com>',
        cc_field='charlie@acme.com',
    )
    item = db.get_item("item-1")
    contacts.scrape_item_headers(item)

    assert db.get_contact_by_email("alice@acme.com")["name"]   == "Alice Smith"
    assert db.get_contact_by_email("bob@acme.com")["name"]     == "Bob Jones"
    assert db.get_contact_by_email("charlie@acme.com") is not None  # bare addr ok


def test_rebuild_from_items_is_idempotent(client):
    _seed_item(client, "item-1", 'Alice <alice@acme.com>', to_field='Bob <bob@acme.com>')
    _seed_item(client, "item-2", 'Alice <alice@acme.com>', to_field='Bob <bob@acme.com>')

    s1 = contacts.rebuild_from_items()
    assert s1["total_contacts"] == 2

    s2 = contacts.rebuild_from_items()
    assert s2["total_contacts"] == 2  # no duplicates

    alice = db.get_contact_by_email("alice@acme.com")
    # Each rebuild bumps source_count by the number of times the email appears
    # across all items — 2 items × 2 rebuilds = 4 sightings.
    assert alice["source_count"] == 4


# ── resolve_owner_email fallback ─────────────────────────────────────────────

def test_resolve_owner_email_in_header_first():
    """When the owner is in To/CC, return immediately without consulting db."""
    email = agent.resolve_owner_email(
        "Mike", "Mike Smith <mike@acme.com>, Other <other@acme.com>"
    )
    assert email == "mike@acme.com"


def test_resolve_owner_email_falls_back_to_contacts_table():
    """When the owner isn't in headers, look up the contacts table."""
    db.upsert_contact_from_header("Mike Smith", "mike@acme.com", item_id="prior")
    # Headers do NOT contain Mike — must come from the contacts table.
    email = agent.resolve_owner_email("Mike", "alice@acme.com, bob@acme.com")
    assert email == "mike@acme.com"


def test_resolve_owner_email_returns_none_when_no_match():
    assert agent.resolve_owner_email("Nobody", "alice@acme.com") is None


# ── HTTP CRUD ─────────────────────────────────────────────────────────────────

def test_create_and_get_contact_via_api(client):
    r = client.post("/contacts", json={
        "name": "Jane Doe",
        "phone": "555-0100",
        "employer": "Acme",
        "title": "Engineer",
        "emails": ["jane@acme.com", "jane@personal.com"],
    })
    assert r.status_code == 200
    contact = r.json()
    assert contact["name"]          == "Jane Doe"
    assert contact["primary_email"] == "jane@acme.com"
    assert set(contact["emails"])   == {"jane@acme.com", "jane@personal.com"}
    assert contact["is_manual"]     == 1

    # GET should return the same row
    cid = contact["contact_id"]
    r2  = client.get(f"/contacts/{cid}")
    assert r2.status_code == 200
    assert r2.json()["name"] == "Jane Doe"


def test_patch_contact(client):
    r   = client.post("/contacts", json={"name": "Jane", "emails": ["jane@a.com"]})
    cid = r.json()["contact_id"]
    r2  = client.patch(f"/contacts/{cid}", json={"title": "Director", "phone": "555-9"})
    assert r2.status_code == 200
    assert r2.json()["title"] == "Director"
    assert r2.json()["phone"] == "555-9"


def test_delete_contact_cascades_emails(client):
    r   = client.post("/contacts", json={"name": "Jane", "emails": ["jane@a.com"]})
    cid = r.json()["contact_id"]
    assert client.delete(f"/contacts/{cid}").status_code == 200
    assert client.get(f"/contacts/{cid}").status_code == 404
    # Email should be available again because the row was cascaded out
    assert db.get_contact_by_email("jane@a.com") is None


def test_add_and_remove_email(client):
    r   = client.post("/contacts", json={"name": "Jane", "emails": ["jane@a.com"]})
    cid = r.json()["contact_id"]
    r2  = client.post(f"/contacts/{cid}/emails", json={"email": "jane@b.com"})
    assert r2.status_code == 200
    assert "jane@b.com" in r2.json()["emails"]
    r3  = client.delete(f"/contacts/{cid}/emails/jane@b.com")
    assert r3.status_code == 200
    assert "jane@b.com" not in r3.json()["emails"]


def test_add_email_already_attached_to_other_contact_returns_409(client):
    a = client.post("/contacts", json={"name": "A", "emails": ["x@y.com"]}).json()
    b = client.post("/contacts", json={"name": "B", "emails": ["b@y.com"]}).json()
    r = client.post(f"/contacts/{b['contact_id']}/emails", json={"email": "x@y.com"})
    assert r.status_code == 409


def test_list_contacts_search(client):
    client.post("/contacts", json={"name": "Jane Doe",   "employer": "Acme",  "emails": ["jane@acme.com"]})
    client.post("/contacts", json={"name": "Bob Smith",  "employer": "Globex","emails": ["bob@globex.com"]})

    r = client.get("/contacts?query=acme")
    names = [c["name"] for c in r.json()["contacts"]]
    assert "Jane Doe" in names
    assert "Bob Smith" not in names


def test_rebuild_endpoint(client):
    _seed_item(client, "item-1", 'Alice <alice@acme.com>', to_field='Bob <bob@acme.com>')
    r = client.post("/contacts/rebuild")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["items_scanned"]  >= 1
    assert body["total_contacts"] >= 2
