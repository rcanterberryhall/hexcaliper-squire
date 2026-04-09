"""tests/test_seed.py — Tests for POST /seed and POST /seed/apply."""
import json
from unittest.mock import patch, MagicMock

import pytest
from app import analyses, settings_tbl


# ── Helpers ───────────────────────────────────────────────────────────────────

def _analysis(item_id, title, summary, source="outlook", priority="medium",
              category="fyi", hierarchy="general", is_passdown=False,
              project_tag=None, body_preview=""):
    return {
        "item_id":      item_id,
        "source":       source,
        "title":        title,
        "summary":      summary,
        "priority":     priority,
        "category":     category,
        "hierarchy":    hierarchy,
        "is_passdown":  is_passdown,
        "project_tag":  project_tag,
        "body_preview": body_preview,
        "timestamp":    "2026-03-17T10:00:00+00:00",
    }


def _seed_corpus():
    """Insert a small realistic corpus spanning two recognisable themes."""
    records = [
        _analysis("r1", "SHIFT HIGHLIGHTS — Reactor A coolant pressure drop",
                  "Reactor A coolant pressure dropped. Restart in progress.",
                  is_passdown=True, priority="high",
                  body_preview="reactor A coolant pressure dropped"),
        _analysis("r2", "Re: Reactor A — PROJ-88 valve inspection",
                  "Valve showing wear. Parts ordered.",
                  priority="high", body_preview="reactor A valve PROJ-88"),
        _analysis("r3", "SHIFT ACTIVITIES — Reactor A stable",
                  "Back online. All parameters nominal.",
                  is_passdown=True, body_preview="reactor A online nominal"),
        _analysis("r4", "PROJ-88 parts arrived — scheduling replacement",
                  "Spares in stores. Scheduling Saturday window.",
                  body_preview="reactor PROJ-88 valve replacement Saturday"),
        _analysis("r5", "Reactor A coolant trending down again",
                  "Pressure at 93%. May need to accelerate PROJ-88.",
                  priority="high", body_preview="reactor A coolant PROJ-88"),

        _analysis("s1", "Safety audit Zone 3 — findings report",
                  "Six findings. Two high-priority items require CAP.",
                  priority="high", body_preview="safety audit zone 3 corrective action CAP"),
        _analysis("s2", "Re: Safety audit — CAP draft submitted",
                  "Draft corrective action plan under review.",
                  body_preview="safety audit corrective action plan"),
        _analysis("s3", "SHIFT HIGHLIGHTS — Safety walkthrough complete",
                  "Daily safety walkthrough done. No new findings.",
                  is_passdown=True, body_preview="safety walkthrough zone 3"),
        _analysis("s4", "Safety audit CAP — sign-off required",
                  "CAP needs signature before regulator deadline.",
                  priority="high", body_preview="safety audit CAP sign-off"),

        _analysis("n1", "Friday lunch menu",
                  "Cafeteria special: lasagne.", priority="low", category="noise",
                  body_preview="cafeteria menu friday lasagne"),
    ]
    for r in records:
        analyses.insert(r)
    return records


def _ollama_mock(response_dict):
    """Return a mock llm.generate response string."""
    return json.dumps(response_dict)


def _seed(client, body=None, mock_fn=None):
    """
    POST /seed then poll GET /seed/status until done or error.
    ``mock_fn`` is called as the ``side_effect`` for ``seeder.llm.generate``
    if provided; otherwise a default no-op mock is used.
    Returns the final status dict.
    """
    import time
    default_rv   = _ollama_mock({"projects": [], "topics": []})
    side_effect  = mock_fn if mock_fn else None
    rv           = default_rv if not mock_fn else None

    with patch("seeder.llm.generate", side_effect=side_effect, return_value=rv):
        r = client.post("/seed", json=body or {})
        assert r.status_code == 200

        # Background thread runs with mock still active; spin until done
        active_states = {"waiting_for_ingest", "analyzing", "reanalyzing", "scanning"}
        for _ in range(200):
            st = client.get("/seed/status").json()
            if st.get("state") not in active_states:
                return st
            time.sleep(0.05)

    raise TimeoutError("seed job did not complete within timeout")


# ── POST /seed ────────────────────────────────────────────────────────────────

def test_seed_empty_db_returns_empty_lists(client):
    body = _seed(client)
    assert body["projects"] == []
    assert body["topics"] == []
    assert body["item_count"] == 0
    assert body["passdown_count"] == 0


def test_seed_passdown_count_is_accurate(client):
    _seed_corpus()
    body = _seed(client)
    assert body["passdown_count"] == 3


def test_seed_item_count_reflects_corpus(client):
    _seed_corpus()
    body = _seed(client)
    assert body["item_count"] == 10


def test_seed_returns_llm_projects_and_topics(client):
    _seed_corpus()
    map_response    = _ollama_mock({"projects": [{"name": "Reactor Upgrade", "keywords": ["reactor", "PROJ-88"]}], "concerns": ["safety audit CAP"]})
    reduce_response = _ollama_mock({"projects": [{"name": "Reactor Upgrade", "keywords": ["reactor", "PROJ-88"]}], "topics": ["safety audit CAP"]})

    call_count = [0]
    def fake_post(url, **kwargs):
        call_count[0] += 1
        return map_response if call_count[0] == 1 else reduce_response

    body = _seed(client, body={"context": "industrial plant"}, mock_fn=fake_post)
    assert any(p["name"] == "Reactor Upgrade" for p in body["projects"])
    assert "safety audit CAP" in body["topics"]


def test_seed_falls_back_to_flat_merge_when_reduce_fails(client):
    _seed_corpus()
    map_response = _ollama_mock({
        "projects": [{"name": "Reactor Upgrade", "keywords": ["reactor"]}],
        "concerns": ["safety audit"],
    })
    bad_reduce = MagicMock()
    bad_reduce.raise_for_status = MagicMock()
    bad_reduce.json.return_value = {"response": "not json {{{"}

    call_count = [0]
    def fake_post(url, **kwargs):
        call_count[0] += 1
        return map_response if call_count[0] <= 1 else bad_reduce

    body = _seed(client, mock_fn=fake_post)
    assert any(p["name"] == "Reactor Upgrade" for p in body["projects"])
    assert "safety audit" in body["topics"]


def test_seed_caps_at_120_items(client):
    for i in range(130):
        analyses.insert(_analysis(f"bulk-{i}", f"Email {i}", f"Summary {i}"))
    body = _seed(client)
    assert body["item_count"] == 120


def test_seed_passdowns_appear_first_in_batches(client):
    """Passdowns should be at the front of the sorted corpus."""
    _seed_corpus()
    first_batch_prompts = []

    def fake_generate(prompt, **kwargs):
        first_batch_prompts.append(prompt)
        return _ollama_mock({"projects": [], "concerns": [], "topics": []})

    _seed(client, mock_fn=fake_generate)

    # The very first map batch should contain at least one passdown marker
    assert first_batch_prompts, "No Ollama calls were made"
    assert "passdown" in first_batch_prompts[0].lower() or "SHIFT" in first_batch_prompts[0]


# ── POST /seed/apply ──────────────────────────────────────────────────────────
# Patch _maybe_form_situation to avoid Ollama HTTP calls during the background
# situation sweep (the closure inside seed_apply resolves it from module globals).

def test_seed_apply_adds_new_projects(client):
    with patch("seeder._maybe_form_situation"):
        r = client.post("/seed/apply", json={
            "projects": [{"name": "Reactor Upgrade", "keywords": ["reactor", "PROJ-88"]}],
            "topics":   ["safety audit"],
            "retag":    False,
        })
    assert r.status_code == 200
    assert r.json()["projects_added"] == 1
    assert r.json()["topics_added"] == 1


def test_seed_apply_skips_duplicate_project_names(client):
    settings_tbl.insert({"projects": [{
        "name": "Reactor Upgrade", "keywords": ["reactor"],
        "channels": [], "learned_keywords": [], "learned_senders": [],
    }]})
    with patch("seeder._maybe_form_situation"):
        r = client.post("/seed/apply", json={
            "projects": [{"name": "Reactor Upgrade", "keywords": ["reactor", "new-kw"]}],
            "topics":   [],
            "retag":    False,
        })
    assert r.json()["projects_added"] == 0


def test_seed_apply_retags_untagged_items(client):
    _seed_corpus()
    with patch("seeder._maybe_form_situation"):
        r = client.post("/seed/apply", json={
            "projects": [{"name": "Reactor Upgrade", "keywords": ["reactor"]}],
            "topics":   [],
            "retag":    True,
        })
    assert r.status_code == 200
    assert r.json()["items_retagged"] > 0
    tagged = [a for a in analyses.all() if a.get("project_tag") == "Reactor Upgrade"]
    assert len(tagged) > 0


def test_seed_apply_adds_tag_to_already_tagged_items(client):
    """Multi-tag: items with existing tags gain new matching tags (additive)."""
    analyses.insert(_analysis("pre-tagged", "Reactor note", "Already tagged",
                               project_tag="Other Project",
                               body_preview="reactor coolant"))
    with patch("seeder._maybe_form_situation"):
        r = client.post("/seed/apply", json={
            "projects": [{"name": "Reactor Upgrade", "keywords": ["reactor"]}],
            "topics":   [],
            "retag":    True,
        })
    rec = next(a for a in analyses.all() if a.get("item_id") == "pre-tagged")
    import db as _db
    tags = _db.parse_project_tags(rec["project_tag"])
    assert "Other Project" in tags
    assert "Reactor Upgrade" in tags


def test_seed_apply_merges_topics_without_duplicates(client):
    settings_tbl.insert({"focus_topics": "safety audit, reactor ops", "projects": []})
    with patch("seeder._maybe_form_situation"):
        r = client.post("/seed/apply", json={
            "projects": [],
            "topics":   ["safety audit", "shift handoff"],
            "retag":    False,
        })
    assert r.json()["topics_added"] == 1   # "safety audit" already present


def test_seed_apply_returns_zero_counts_for_empty_input(client):
    with patch("seeder._maybe_form_situation"):
        r = client.post("/seed/apply", json={"projects": [], "topics": [], "retag": False})
    assert r.status_code == 200
    body = r.json()
    assert body["projects_added"] == 0
    assert body["topics_added"] == 0
    assert body["items_retagged"] == 0
