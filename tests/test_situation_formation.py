"""tests/test_situation_formation.py — Unit tests for situation formation logic.

Exercises _maybe_form_situation, _rescore_situation, and _update_situation_record
directly. The existing test_situations.py only tests HTTP endpoints against
pre-inserted fixture data; it never exercises the formation logic itself.
"""
import json
import uuid
from unittest.mock import patch

from situation_manager import (
    _maybe_form_situation,
    _rescore_situation,
    _update_situation_record,
)
from app import (
    analyses,
    situations_tbl,
    intel_tbl,
    scan_state,
    Q,
)

MOCK_SYNTHESIS = {
    "title":        "Test Situation",
    "summary":      "Two related items detected.",
    "status":       "in_progress",
    "open_actions": [],
    "key_context":  None,
}


def _insert_analysis(
    item_id,
    source="jira",
    priority="medium",
    hierarchy="project",
    project_tag=None,
    refs=None,
    timestamp=None,
    situation_id=None,
):
    analyses.insert({
        "item_id":           item_id,
        "source":            source,
        "title":             f"Item {item_id}",
        "author":            "alice",
        "timestamp":         timestamp or "2026-03-17T10:00:00+00:00",
        "url":               "",
        "has_action":        False,
        "priority":          priority,
        "category":          "fyi",
        "summary":           f"Summary of {item_id}",
        "urgency":           None,
        "action_items":      "[]",
        "goals":             "[]",
        "key_dates":         "[]",
        "information_items": "[]",
        "body_preview":      f"body of {item_id}",
        "hierarchy":         hierarchy,
        "project_tag":       project_tag,
        "references":        json.dumps(refs or []),
        "situation_id":      situation_id,
        "processed_at":      "2026-03-17T10:00:00+00:00",
    })


def _insert_situation(
    sit_id,
    item_ids,
    project_tag=None,
    score=1.0,
    score_updated_at="2026-03-17T10:00:00+00:00",
):
    situations_tbl.insert({
        "situation_id":     sit_id,
        "title":            "Existing Situation",
        "summary":          "Things are happening.",
        "status":           "in_progress",
        "item_ids":         item_ids,
        "sources":          ["jira"],
        "project_tag":      project_tag,
        "score":            score,
        "priority":         "medium",
        "open_actions":     [],
        "references":       [],
        "key_context":      None,
        "last_updated":     "2026-03-17T10:00:00+00:00",
        "created_at":       "2026-03-17T10:00:00+00:00",
        "score_updated_at": score_updated_at,
        "dismissed":        False,
    })


# ── Formation from shared references ─────────────────────────────────────────

def test_forms_situation_from_shared_reference():
    _insert_analysis("a1", refs=["proj-42"])
    _insert_analysis("a2", refs=["proj-42"])

    with patch("situation_manager._correlator.synthesize_situation", return_value=MOCK_SYNTHESIS):
        _maybe_form_situation("a1")

    all_sits = situations_tbl.all()
    assert len(all_sits) == 1
    sit = all_sits[0]
    assert "a1" in sit["item_ids"]
    assert "a2" in sit["item_ids"]

    sit_id = sit["situation_id"]
    r1 = analyses.get(Q.item_id == "a1")
    r2 = analyses.get(Q.item_id == "a2")
    assert r1["situation_id"] == sit_id
    assert r2["situation_id"] == sit_id


def test_does_not_form_situation_with_no_candidates():
    _insert_analysis("solo", refs=[])
    _maybe_form_situation("solo")
    assert situations_tbl.all() == []


def test_merges_new_item_into_existing_situation():
    sit_id = str(uuid.uuid4())
    _insert_analysis("a1", refs=["proj-99"], situation_id=sit_id)
    _insert_analysis("a2", refs=["proj-99"], situation_id=sit_id)
    _insert_situation(sit_id, ["a1", "a2"])
    _insert_analysis("a3", refs=["proj-99"])

    with patch("situation_manager._correlator.synthesize_situation", return_value=MOCK_SYNTHESIS):
        _maybe_form_situation("a3")

    all_sits = situations_tbl.all()
    assert len(all_sits) == 1
    merged = all_sits[0]
    assert "a1" in merged["item_ids"]
    assert "a2" in merged["item_ids"]
    assert "a3" in merged["item_ids"]
    assert analyses.get(Q.item_id == "a3")["situation_id"] == merged["situation_id"]


def test_does_not_form_situation_below_minimum_cluster_size():
    """Delete the second analysis after insertion so its record is absent when
    _maybe_form_situation looks up the cluster, triggering the len < 2 guard."""
    _insert_analysis("a1", refs=["proj-55"])
    _insert_analysis("a2", refs=["proj-55"])
    analyses.remove(Q.item_id == "a2")

    _maybe_form_situation("a1")
    assert situations_tbl.all() == []


def test_rescores_existing_situation_when_item_already_member():
    """When an item with no cross-refs is already a situation member, calling
    _maybe_form_situation should rescore (not create a new situation) and update
    score_updated_at."""
    sit_id = str(uuid.uuid4())
    _insert_analysis("a1", refs=[], situation_id=sit_id)
    _insert_analysis("a2", refs=[], situation_id=sit_id)
    _insert_situation(sit_id, ["a1", "a2"],
                      score_updated_at="2020-01-01T00:00:00+00:00")

    _maybe_form_situation("a1")

    updated = situations_tbl.get(Q.situation_id == sit_id)
    assert updated["score_updated_at"] != "2020-01-01T00:00:00+00:00"
    # No additional situations should have been created
    assert len(situations_tbl.all()) == 1


def test_rescore_situation_updates_score_field():
    sit_id = str(uuid.uuid4())
    _insert_analysis("b1", priority="high", hierarchy="user",
                     timestamp="2026-03-25T08:00:00+00:00")
    _insert_analysis("b2", priority="high", hierarchy="user",
                     timestamp="2026-03-25T08:00:00+00:00")
    _insert_situation(sit_id, ["b1", "b2"], score=0.0,
                      score_updated_at="2020-01-01T00:00:00+00:00")

    _rescore_situation(sit_id)

    updated = situations_tbl.get(Q.situation_id == sit_id)
    assert updated["score"] > 0.0
    assert updated["score_updated_at"] != "2020-01-01T00:00:00+00:00"


def test_update_situation_record_runs_synthesis():
    sit_id = str(uuid.uuid4())
    _insert_analysis("c1")
    _insert_analysis("c2")
    _insert_situation(sit_id, ["c1", "c2"])

    distinctive = {**MOCK_SYNTHESIS, "title": "Synthesized Title XYZ"}
    with patch("situation_manager._correlator.synthesize_situation", return_value=distinctive):
        _update_situation_record(sit_id, ["c1", "c2"])

    updated = situations_tbl.get(Q.situation_id == sit_id)
    assert updated["title"] == "Synthesized Title XYZ"


def test_situation_not_formed_when_cancelled():
    """The cancellation guard inside _maybe_form_situation must prevent situation
    creation even when a valid cluster has been found."""
    scan_state["cancelled"] = True
    try:
        _insert_analysis("d1", refs=["proj-77"])
        _insert_analysis("d2", refs=["proj-77"])
        _maybe_form_situation("d1")
        assert situations_tbl.all() == []
    finally:
        scan_state["cancelled"] = False


def test_situation_project_tag_consensus():
    """Situation project_tag should equal the shared tag when all members agree,
    and become None once a member from a different project is merged in."""
    _insert_analysis("e1", refs=["proj-88"], project_tag="alpha")
    _insert_analysis("e2", refs=["proj-88"], project_tag="alpha")
    _insert_analysis("e3", refs=["proj-88"], project_tag="alpha")

    with patch("situation_manager._correlator.synthesize_situation", return_value=MOCK_SYNTHESIS):
        _maybe_form_situation("e1")

    sit = situations_tbl.all()[0]
    assert sit["project_tag"] == "alpha"

    # Merge a fourth item tagged to a different project — breaks consensus
    _insert_analysis("e4", project_tag="beta")
    all_ids = sit["item_ids"] + ["e4"]

    with patch("situation_manager._correlator.synthesize_situation", return_value=MOCK_SYNTHESIS):
        _update_situation_record(sit["situation_id"], all_ids)

    updated = situations_tbl.get(Q.situation_id == sit["situation_id"])
    assert updated["project_tag"] is None
