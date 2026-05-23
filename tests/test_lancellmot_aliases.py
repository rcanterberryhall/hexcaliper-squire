"""Tests for lancellmot_aliases table + CRUD helpers (parsival#43)."""
import db


def test_lancellmot_aliases_table_exists():
    cols = {r[1] for r in db.conn().execute(
        "PRAGMA table_info(lancellmot_aliases)"
    ).fetchall()}
    assert cols == {
        "parsival_project",
        "lancellmot_project_id",
        "lancellmot_project_name",
        "created_at",
        "updated_at",
    }


def test_upsert_creates_alias():
    db.upsert_lancellmot_alias(
        parsival_project="Ethylene-Cracker-3",
        lancellmot_project_id="proj-123",
        lancellmot_project_name="ethylene-cracker-3",
    )
    row = db.get_lancellmot_alias_for_tag("Ethylene-Cracker-3")
    assert row is not None
    assert row["lancellmot_project_id"] == "proj-123"
    assert row["lancellmot_project_name"] == "ethylene-cracker-3"
    assert row["created_at"]
    assert row["updated_at"]


def test_upsert_updates_existing_alias():
    db.upsert_lancellmot_alias(
        parsival_project="Alpha",
        lancellmot_project_id="old-id",
        lancellmot_project_name="old-name",
    )
    original = db.get_lancellmot_alias_for_tag("Alpha")
    db.upsert_lancellmot_alias(
        parsival_project="Alpha",
        lancellmot_project_id="new-id",
        lancellmot_project_name="new-name",
    )
    updated = db.get_lancellmot_alias_for_tag("Alpha")
    assert updated["lancellmot_project_id"] == "new-id"
    assert updated["lancellmot_project_name"] == "new-name"
    assert updated["created_at"] == original["created_at"]
    assert updated["updated_at"] >= original["updated_at"]


def test_get_lancellmot_alias_returns_none_for_missing():
    assert db.get_lancellmot_alias_for_tag("Nonexistent") is None


def test_list_lancellmot_aliases_returns_all():
    db.upsert_lancellmot_alias("A", "idA", "nameA")
    db.upsert_lancellmot_alias("B", "idB", "nameB")
    rows = db.list_lancellmot_aliases()
    names = {r["parsival_project"] for r in rows}
    assert {"A", "B"}.issubset(names)


def test_delete_lancellmot_alias_removes_row():
    db.upsert_lancellmot_alias("Doomed", "id-doom", "name-doom")
    assert db.get_lancellmot_alias_for_tag("Doomed") is not None
    db.delete_lancellmot_alias("Doomed")
    assert db.get_lancellmot_alias_for_tag("Doomed") is None


def test_delete_lancellmot_alias_missing_is_noop():
    db.delete_lancellmot_alias("Never-Existed")  # must not raise
