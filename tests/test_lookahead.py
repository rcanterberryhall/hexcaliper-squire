"""Tests for the look-ahead board (parsival#48, #49, #50).

Covers CRUD, templates, instantiation, reschedule, detach, auto-complete,
and the cross-system LLM linking suggestion pool.
"""
from unittest.mock import patch

import db


def _wipe_lookahead():
    c = db.conn()
    for tbl in ("lookahead_card_link_suggestions",
                "lookahead_card_resources", "lookahead_card_links",
                "lookahead_card_deps", "lookahead_cards",
                "lookahead_template_task_resources",
                "lookahead_template_task_deps",
                "lookahead_template_tasks",
                "lookahead_template_instances",
                "lookahead_templates",
                "lookahead_resources", "project_shifts"):
        c.execute(f"DELETE FROM {tbl}")


def _card_payload(**overrides):
    body = {
        "title":           "Install panel",
        "project":         "P905",
        "assignee":        "Alice",
        "start_date":      "2026-04-15",
        "start_shift_num": 1,
        "end_date":        "2026-04-16",
        "end_shift_num":   2,
        "status":          "planned",
    }
    body.update(overrides)
    return body


# ── Card CRUD ─────────────────────────────────────────────────────────────────

def test_create_card_generates_uuid(client):
    _wipe_lookahead()
    r = client.post("/lookahead/cards", json=_card_payload())
    assert r.status_code == 200
    card = r.json()
    assert card["id"] and len(card["id"]) >= 32   # UUID string
    assert card["title"] == "Install panel"
    assert card["status"] == "planned"


def test_create_card_validates_required_fields(client):
    _wipe_lookahead()
    r = client.post("/lookahead/cards", json={"title": "no dates"})
    assert r.status_code == 400


def test_create_card_rejects_inverted_dates(client):
    _wipe_lookahead()
    r = client.post("/lookahead/cards", json=_card_payload(
        start_date="2026-04-20", end_date="2026-04-18"))
    assert r.status_code == 400


def test_create_card_rejects_bad_status(client):
    _wipe_lookahead()
    r = client.post("/lookahead/cards", json=_card_payload(status="on_fire"))
    assert r.status_code == 400


def test_list_cards_filters_by_project_and_window(client):
    _wipe_lookahead()
    client.post("/lookahead/cards", json=_card_payload(title="A", project="P905",
        start_date="2026-04-10", end_date="2026-04-11"))
    client.post("/lookahead/cards", json=_card_payload(title="B", project="P905",
        start_date="2026-04-20", end_date="2026-04-21"))
    client.post("/lookahead/cards", json=_card_payload(title="C", project="OTHER",
        start_date="2026-04-15", end_date="2026-04-15"))

    all_p905 = client.get("/lookahead/cards?project=P905").json()
    assert {c["title"] for c in all_p905} == {"A", "B"}

    windowed = client.get("/lookahead/cards?project=P905"
                          "&start=2026-04-15&end=2026-04-25").json()
    assert {c["title"] for c in windowed} == {"B"}


def test_patch_card_preserves_relations(client):
    _wipe_lookahead()
    card = client.post("/lookahead/cards", json=_card_payload()).json()
    client.patch(f"/lookahead/cards/{card['id']}", json={"status": "in_progress"})
    refreshed = client.get(f"/lookahead/cards/{card['id']}").json()
    assert refreshed["status"] == "in_progress"
    assert refreshed["title"] == card["title"]  # other fields intact


def test_delete_card_returns_ok(client):
    _wipe_lookahead()
    card = client.post("/lookahead/cards", json=_card_payload()).json()
    r = client.delete(f"/lookahead/cards/{card['id']}")
    assert r.json()["ok"] is True
    assert client.get(f"/lookahead/cards/{card['id']}").status_code == 404


# ── Dependencies / links / resources on cards ────────────────────────────────

def test_card_dependencies_are_round_trippable(client):
    _wipe_lookahead()
    a = client.post("/lookahead/cards", json=_card_payload(title="A")).json()
    b = client.post("/lookahead/cards",
                    json=_card_payload(title="B", depends_on=[a["id"]])).json()
    assert b["depends_on"] == [a["id"]]

    # replacement semantics: PATCH with empty list clears deps
    client.patch(f"/lookahead/cards/{b['id']}", json={"depends_on": []})
    assert client.get(f"/lookahead/cards/{b['id']}").json()["depends_on"] == []


def test_card_self_dependency_ignored(client):
    _wipe_lookahead()
    a = client.post("/lookahead/cards", json=_card_payload(title="A")).json()
    client.patch(f"/lookahead/cards/{a['id']}", json={"depends_on": [a["id"]]})
    assert client.get(f"/lookahead/cards/{a['id']}").json()["depends_on"] == []


def test_card_deps_cascade_on_delete(client):
    _wipe_lookahead()
    a = client.post("/lookahead/cards", json=_card_payload(title="A")).json()
    b = client.post("/lookahead/cards",
                    json=_card_payload(title="B", depends_on=[a["id"]])).json()
    client.delete(f"/lookahead/cards/{a['id']}")
    refreshed = client.get(f"/lookahead/cards/{b['id']}").json()
    assert refreshed["depends_on"] == []


def test_card_links_validate_type(client):
    _wipe_lookahead()
    card = client.post("/lookahead/cards", json=_card_payload(
        links=[{"type": "todo", "id": "42"},
               {"type": "bogus", "id": "1"}])).json()
    types = {l["type"] for l in card["links"]}
    assert types == {"todo"}


def test_card_bom_and_resource_status_update(client):
    _wipe_lookahead()
    bob = client.post("/lookahead/resources",
                      json={"name": "Bob", "type": "person"}).json()
    card = client.post("/lookahead/cards", json=_card_payload(
        resources=[{"resource_id": bob["id"], "quantity": 1, "status": "needed"}])).json()
    assert len(card["resources"]) == 1
    assert card["resources"][0]["status"] == "needed"

    r = client.patch(f"/lookahead/cards/{card['id']}/resources/{bob['id']}",
                     json={"status": "secured"})
    updated = r.json()
    assert updated["resources"][0]["status"] == "secured"


# ── Resource catalog ─────────────────────────────────────────────────────────

def test_resource_crud_cycle(client):
    _wipe_lookahead()
    r = client.post("/lookahead/resources",
                    json={"name": "Crane", "type": "equipment"})
    res = r.json()
    assert res["name"] == "Crane" and res["type"] == "equipment"

    client.patch(f"/lookahead/resources/{res['id']}",
                 json={"notes": "5-ton"})
    fetched = [x for x in client.get("/lookahead/resources").json()
               if x["id"] == res["id"]][0]
    assert fetched["notes"] == "5-ton"

    client.delete(f"/lookahead/resources/{res['id']}")
    remaining = [x for x in client.get("/lookahead/resources").json()
                 if x["id"] == res["id"]]
    assert remaining == []


def test_resource_rejects_invalid_type(client):
    _wipe_lookahead()
    r = client.post("/lookahead/resources",
                    json={"name": "X", "type": "nonsense"})
    assert r.status_code == 400


def test_resource_list_filters_by_type(client):
    _wipe_lookahead()
    client.post("/lookahead/resources", json={"name": "Alice", "type": "person"})
    client.post("/lookahead/resources", json={"name": "Crane", "type": "equipment"})
    only_people = client.get("/lookahead/resources?type=person").json()
    assert [r["name"] for r in only_people] == ["Alice"]


# ── Project shift schedules ──────────────────────────────────────────────────

def test_shift_upsert_and_list(client):
    _wipe_lookahead()
    client.put("/lookahead/shifts/P905/1", json={
        "label": "1st", "start_time": "06:00", "end_time": "16:00",
        "days": "M,T,W,Th",
    })
    client.put("/lookahead/shifts/P905/2", json={
        "label": "2nd", "start_time": "14:00", "end_time": "24:00",
        "days": "M,T,W,Th",
    })
    shifts = client.get("/lookahead/shifts?project=P905").json()
    assert len(shifts) == 2
    assert shifts[0]["shift_num"] == 1 and shifts[0]["start_time"] == "06:00"

    # Upsert overwrites
    client.put("/lookahead/shifts/P905/1",
               json={"label": "1st updated", "start_time": "07:00",
                     "end_time": "17:00", "days": "M,T,W,Th,F"})
    reread = client.get("/lookahead/shifts?project=P905").json()[0]
    assert reread["label"] == "1st updated" and reread["days"] == "M,T,W,Th,F"


def test_shift_rejects_invalid_num(client):
    _wipe_lookahead()
    r = client.put("/lookahead/shifts/P905/7", json={"label": "x"})
    assert r.status_code == 400


def test_shift_delete_scoped_to_project(client):
    _wipe_lookahead()
    client.put("/lookahead/shifts/P905/1", json={"label": "1st"})
    client.put("/lookahead/shifts/OTHER/1", json={"label": "1st-other"})
    client.delete("/lookahead/shifts/P905/1")
    remaining = client.get("/lookahead/shifts").json()
    assert [s["project_tag"] for s in remaining] == ["OTHER"]


# ── Overview ─────────────────────────────────────────────────────────────────

def test_overview_groups_and_sorts_by_earliest(client):
    _wipe_lookahead()
    client.post("/lookahead/cards", json=_card_payload(project="LATE",
        start_date="2026-04-20", end_date="2026-04-21"))
    client.post("/lookahead/cards", json=_card_payload(project="EARLY",
        start_date="2026-04-10", end_date="2026-04-11"))
    rows = client.get("/lookahead/overview").json()
    assert [r["project"] for r in rows] == ["EARLY", "LATE"]
    assert len(rows[0]["cards"]) == 1


# ── Templates (parsival#49) ───────────────────────────────────────────────────

def _template_payload(**overrides):
    """Two-task template: A (day 0, 1 shift), B (day 2, 2 shifts) depends on A."""
    body = {
        "name": "Weekly inspection",
        "description": "Routine weekly check",
        "owner": "Alice",
        "duration_unit": "calendar_days",
        "default_project_tag": "P905",
        "tasks": [
            {
                "local_id": "A",
                "title": "Pre-check",
                "offset_start_days": 0,
                "offset_start_shift": 1,
                "duration_shifts": 1,
            },
            {
                "local_id": "B",
                "title": "Main inspection",
                "offset_start_days": 2,
                "offset_start_shift": 1,
                "duration_shifts": 2,
                "depends_on": ["A"],
            },
        ],
    }
    body.update(overrides)
    return body


def test_template_create_and_round_trip(client):
    _wipe_lookahead()
    r = client.post("/lookahead/templates", json=_template_payload())
    assert r.status_code == 200
    tpl = r.json()
    assert tpl["name"] == "Weekly inspection"
    assert tpl["version"] == 1
    assert len(tpl["tasks"]) == 2
    task_b = [t for t in tpl["tasks"] if t["local_id"] == "B"][0]
    assert task_b["depends_on"] == ["A"]


def test_template_requires_name(client):
    _wipe_lookahead()
    r = client.post("/lookahead/templates", json={"tasks": []})
    assert r.status_code == 400


def test_template_rejects_invalid_duration_unit(client):
    _wipe_lookahead()
    r = client.post("/lookahead/templates",
                    json=_template_payload(duration_unit="weeks"))
    assert r.status_code == 400


def test_template_patch_bumps_version_and_replaces_tasks(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    assert tpl["version"] == 1
    r = client.patch(f"/lookahead/templates/{tpl['id']}", json={
        "description": "Updated",
        "tasks": [{"local_id": "solo", "title": "Just one",
                   "offset_start_days": 0, "duration_shifts": 1}],
    })
    refreshed = r.json()
    assert refreshed["version"] == 2
    assert refreshed["description"] == "Updated"
    assert [t["local_id"] for t in refreshed["tasks"]] == ["solo"]


def test_template_delete_cascades_tasks(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    client.delete(f"/lookahead/templates/{tpl['id']}")
    assert client.get(f"/lookahead/templates/{tpl['id']}").status_code == 404


def test_instantiate_materializes_cards_with_correct_dates(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    r = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                    json={"start_date": "2026-05-04", "project_tag": "P905"})
    assert r.status_code == 200
    inst = r.json()
    assert inst["status"] == "active"
    assert inst["template_version"] == 1
    cards_by_local = {c["template_task_local_id"]: c for c in inst["cards"]}
    # Task A: day 0, shift 1, duration 1 → starts & ends 2026-05-04 shift 1
    assert cards_by_local["A"]["start_date"] == "2026-05-04"
    assert cards_by_local["A"]["end_date"]   == "2026-05-04"
    assert cards_by_local["A"]["start_shift_num"] == 1
    assert cards_by_local["A"]["end_shift_num"]   == 1
    # Task B: day 2, shift 1, duration 2 → starts 2026-05-06, ends 2026-05-06 shift 2
    assert cards_by_local["B"]["start_date"] == "2026-05-06"
    assert cards_by_local["B"]["end_date"]   == "2026-05-06"
    assert cards_by_local["B"]["end_shift_num"] == 2
    # Dep wiring: B depends_on [A.id]
    assert cards_by_local["B"]["depends_on"] == [cards_by_local["A"]["id"]]


def test_instantiate_business_days_skips_weekends(client):
    _wipe_lookahead()
    body = _template_payload(duration_unit="business_days",
                             tasks=[{"local_id": "x", "title": "X",
                                     "offset_start_days": 3, "duration_shifts": 1}])
    tpl = client.post("/lookahead/templates", json=body).json()
    # 2026-05-04 is a Monday → +3 business days → 2026-05-07 (Thu)
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    assert inst["cards"][0]["start_date"] == "2026-05-07"


# ── Per-task work_days mask (parsival#73) ────────────────────────────────────

def test_card_work_days_round_trips(client):
    _wipe_lookahead()
    card = client.post("/lookahead/cards", json=_card_payload(
        work_days="M,T,W,Th,F")).json()
    assert card["work_days"] == "M,T,W,Th,F"
    fetched = client.get(f"/lookahead/cards/{card['id']}").json()
    assert fetched["work_days"] == "M,T,W,Th,F"
    # PATCH to a new mask persists.
    client.patch(f"/lookahead/cards/{card['id']}", json={"work_days": "Su,M,T,W"})
    assert client.get(f"/lookahead/cards/{card['id']}").json()["work_days"] == "Su,M,T,W"
    # Empty string clears it (all 7 days).
    client.patch(f"/lookahead/cards/{card['id']}", json={"work_days": ""})
    assert client.get(f"/lookahead/cards/{card['id']}").json()["work_days"] == ""


def test_instantiate_per_task_work_days_overrides_unit(client):
    _wipe_lookahead()
    # Template runs on calendar_days, but task X restricts to Mon-Thu.
    body = _template_payload(duration_unit="calendar_days",
                             tasks=[{"local_id": "x", "title": "X",
                                     "offset_start_days": 4, "duration_shifts": 1,
                                     "work_days": "M,T,W,Th"}])
    tpl = client.post("/lookahead/templates", json=body).json()
    # 2026-05-04 Mon → +4 Mon-Thu days → Mon May 4 (cur), Tue, Wed, Thu, Mon May 11.
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    card = inst["cards"][0]
    assert card["start_date"] == "2026-05-11"
    assert card["work_days"] == "M,T,W,Th"


def test_instantiate_sunday_wednesday_workweek(client):
    _wipe_lookahead()
    # Sun-Wed shift crew: +3 work-days from Sun → Sun, Mon, Tue, Wed.
    body = _template_payload(duration_unit="calendar_days",
                             tasks=[{"local_id": "x", "title": "X",
                                     "offset_start_days": 3, "duration_shifts": 1,
                                     "work_days": "Su,M,T,W"}])
    tpl = client.post("/lookahead/templates", json=body).json()
    # 2026-05-03 is a Sunday → +3 Sun-Wed days → 2026-05-06 (Wed).
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-03",
                             "project_tag": "P905"}).json()
    assert inst["cards"][0]["start_date"] == "2026-05-06"


def test_reschedule_honors_per_card_work_days(client):
    _wipe_lookahead()
    # Two tasks, different workweeks: one unrestricted, one Mon-Fri.
    body = _template_payload(duration_unit="calendar_days", tasks=[
        {"local_id": "A", "title": "Any day",
         "offset_start_days": 0, "duration_shifts": 1},
        {"local_id": "B", "title": "Weekdays only",
         "offset_start_days": 0, "duration_shifts": 1,
         "work_days": "M,T,W,Th,F"},
    ])
    tpl = client.post("/lookahead/templates", json=body).json()
    # Start 2026-05-04 Mon; both cards land on Mon.
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    by_local = {c["template_task_local_id"]: c for c in inst["cards"]}
    assert by_local["A"]["start_date"] == "2026-05-04"
    assert by_local["B"]["start_date"] == "2026-05-04"
    # Reschedule by +6 calendar days to 2026-05-10 (Sun).
    rescheduled = client.patch(f"/lookahead/instances/{inst['id']}",
                               json={"start_date": "2026-05-10"}).json()
    by_local = {c["template_task_local_id"]: c for c in rescheduled["cards"]}
    # Unrestricted card follows calendar delta → Sun 2026-05-10.
    assert by_local["A"]["start_date"] == "2026-05-10"
    # Mon-Fri card counts its own work-days in the (Mon 05-04, Sun 05-10]
    # window — Tue/Wed/Thu/Fri = 4 — so Mon + 4 business days → Fri 2026-05-08.
    assert by_local["B"]["start_date"] == "2026-05-08"


def test_upgrade_instance_propagates_work_days(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    card_a = [c for c in inst["cards"] if c["template_task_local_id"] == "A"][0]
    assert card_a["work_days"] == ""
    # Template author adds a work_days mask to A.
    client.patch(f"/lookahead/templates/{tpl['id']}", json={"tasks": [
        {"local_id": "A", "title": "Pre-check",
         "offset_start_days": 0, "offset_start_shift": 1, "duration_shifts": 1,
         "work_days": "M,T,W,Th,F"},
        {"local_id": "B", "title": "Main inspection",
         "offset_start_days": 2, "offset_start_shift": 1, "duration_shifts": 2,
         "depends_on": ["A"]},
    ]})
    upgraded = client.post(f"/lookahead/instances/{inst['id']}/upgrade").json()
    by_local = {c["template_task_local_id"]: c for c in upgraded["cards"]}
    assert by_local["A"]["work_days"] == "M,T,W,Th,F"
    assert by_local["B"]["work_days"] == ""


def test_instantiate_copies_named_resources_to_bom(client):
    _wipe_lookahead()
    crane = client.post("/lookahead/resources",
                        json={"name": "Crane", "type": "equipment"}).json()
    body = _template_payload(tasks=[{
        "local_id": "x", "title": "Lift",
        "offset_start_days": 0, "duration_shifts": 1,
        "resource_requirements": [
            {"resource_type": "equipment",
             "named_resource_id": crane["id"], "quantity": 1},
            {"resource_type": "person", "role": "rigger", "quantity": 2},
        ],
    }])
    tpl = client.post("/lookahead/templates", json=body).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    card = inst["cards"][0]
    # Named requirement → BOM entry; generic role-only requirement is skipped
    # at instantiation time (user assigns it later by editing the card).
    assert len(card["resources"]) == 1
    assert card["resources"][0]["resource_id"] == crane["id"]


def test_reschedule_instance_shifts_all_cards(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    r = client.patch(f"/lookahead/instances/{inst['id']}",
                     json={"start_date": "2026-05-11"})
    rescheduled = r.json()
    by_local = {c["template_task_local_id"]: c for c in rescheduled["cards"]}
    # Shift is +7 calendar days.
    assert by_local["A"]["start_date"] == "2026-05-11"
    assert by_local["B"]["start_date"] == "2026-05-13"


def test_detach_card_keeps_it_out_of_reschedule(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    detached_card = [c for c in inst["cards"]
                     if c["template_task_local_id"] == "A"][0]
    client.post(f"/lookahead/cards/{detached_card['id']}/detach")
    client.patch(f"/lookahead/instances/{inst['id']}",
                 json={"start_date": "2026-05-11"})
    # Re-fetch the detached card directly — it should not have moved.
    fresh = client.get(f"/lookahead/cards/{detached_card['id']}").json()
    assert fresh["start_date"] == "2026-05-04"
    assert fresh["template_instance_id"] is None


def test_instance_autocompletes_when_all_cards_done(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    for card in inst["cards"][:-1]:
        client.patch(f"/lookahead/cards/{card['id']}", json={"status": "done"})
    # Not yet complete — one card still planned.
    assert client.get(f"/lookahead/instances/{inst['id']}").json()["status"] == "active"
    # Flip the last one.
    client.patch(f"/lookahead/cards/{inst['cards'][-1]['id']}",
                 json={"status": "done"})
    assert client.get(f"/lookahead/instances/{inst['id']}").json()["status"] == "complete"


def test_instance_lists_outdated_flag_after_template_edit(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                json={"start_date": "2026-05-04", "project_tag": "P905"})
    fresh = client.get("/lookahead/instances").json()[0]
    assert fresh["outdated"] is False
    assert fresh["template_current_version"] == 1
    # Bump the template; the instance should now report itself outdated
    # without us having to touch it.
    client.patch(f"/lookahead/templates/{tpl['id']}", json={"description": "v2"})
    after = client.get("/lookahead/instances").json()[0]
    assert after["outdated"] is True
    assert after["template_version"] == 1
    assert after["template_current_version"] == 2


def test_upgrade_instance_refreshes_template_fields_and_preserves_user_edits(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    card_a = [c for c in inst["cards"] if c["template_task_local_id"] == "A"][0]
    # User edits A: assign someone, add notes, mark in_progress.
    client.patch(f"/lookahead/cards/{card_a['id']}", json={
        "assignee": "Bob", "notes": "Bob is on it", "status": "in_progress"})
    # Template author rewrites A: new title, slid to day 1, new procedure doc.
    client.patch(f"/lookahead/templates/{tpl['id']}", json={"tasks": [
        {"local_id": "A", "title": "Pre-check (revised)",
         "offset_start_days": 1, "offset_start_shift": 1, "duration_shifts": 1,
         "linked_procedure_doc": "https://docs/sop-A2"},
        {"local_id": "B", "title": "Main inspection",
         "offset_start_days": 2, "offset_start_shift": 1, "duration_shifts": 2,
         "depends_on": ["A"]},
    ]})
    upgraded = client.post(
        f"/lookahead/instances/{inst['id']}/upgrade").json()
    assert upgraded["template_version"] == 2
    assert upgraded["outdated"] is False
    by_local = {c["template_task_local_id"]: c for c in upgraded["cards"]}
    a = by_local["A"]
    assert a["title"] == "Pre-check (revised)"
    assert a["start_date"] == "2026-05-05"  # slid by +1 calendar day
    assert a["linked_procedure_doc"] == "https://docs/sop-A2"
    assert a["assignee"] == "Bob"             # preserved
    assert a["notes"] == "Bob is on it"       # preserved
    assert a["status"] == "in_progress"        # preserved


def test_upgrade_instance_creates_card_for_newly_added_task(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    assert len(inst["cards"]) == 2
    # Add a new task C to the template.
    client.patch(f"/lookahead/templates/{tpl['id']}", json={"tasks": [
        {"local_id": "A", "title": "Pre-check",
         "offset_start_days": 0, "offset_start_shift": 1, "duration_shifts": 1},
        {"local_id": "B", "title": "Main inspection",
         "offset_start_days": 2, "offset_start_shift": 1, "duration_shifts": 2,
         "depends_on": ["A"]},
        {"local_id": "C", "title": "Sign-off",
         "offset_start_days": 4, "offset_start_shift": 1, "duration_shifts": 1,
         "depends_on": ["B"]},
    ]})
    upgraded = client.post(
        f"/lookahead/instances/{inst['id']}/upgrade").json()
    by_local = {c["template_task_local_id"]: c for c in upgraded["cards"]}
    assert "C" in by_local
    c_card = by_local["C"]
    assert c_card["title"] == "Sign-off"
    assert c_card["start_date"] == "2026-05-08"
    assert c_card["depends_on"] == [by_local["B"]["id"]]


def test_upgrade_instance_leaves_orphaned_card_attached(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    card_b = [c for c in inst["cards"] if c["template_task_local_id"] == "B"][0]
    # Drop task B from the template.  Its card already exists and may be in
    # flight, so the upgrade must leave it intact rather than deleting work.
    client.patch(f"/lookahead/templates/{tpl['id']}", json={"tasks": [
        {"local_id": "A", "title": "Pre-check",
         "offset_start_days": 0, "offset_start_shift": 1, "duration_shifts": 1},
    ]})
    client.post(f"/lookahead/instances/{inst['id']}/upgrade")
    fresh = client.get(f"/lookahead/cards/{card_b['id']}")
    assert fresh.status_code == 200
    assert fresh.json()["template_instance_id"] == inst["id"]


def test_upgrade_instance_adds_missing_required_resource_without_dropping_user_bom(client):
    _wipe_lookahead()
    crane = client.post("/lookahead/resources",
                        json={"name": "Crane", "type": "equipment"}).json()
    welder = client.post("/lookahead/resources",
                         json={"name": "Welder", "type": "equipment"}).json()
    body = _template_payload(tasks=[{
        "local_id": "x", "title": "Lift",
        "offset_start_days": 0, "duration_shifts": 1,
        "resource_requirements": [
            {"resource_type": "equipment",
             "named_resource_id": crane["id"], "quantity": 1},
        ],
    }])
    tpl = client.post("/lookahead/templates", json=body).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    card = inst["cards"][0]
    # User adds welder as an extra BOM entry on the card (PATCH replaces
    # the full resource list, so include the existing crane row too).
    client.patch(f"/lookahead/cards/{card['id']}", json={"resources": [
        {"resource_id": crane["id"], "quantity": 1, "status": "needed"},
        {"resource_id": welder["id"], "quantity": 1, "status": "secured"},
    ]})
    # Template author also requires welder going forward.
    client.patch(f"/lookahead/templates/{tpl['id']}", json={"tasks": [{
        "local_id": "x", "title": "Lift",
        "offset_start_days": 0, "duration_shifts": 1,
        "resource_requirements": [
            {"resource_type": "equipment",
             "named_resource_id": crane["id"], "quantity": 1},
            {"resource_type": "equipment",
             "named_resource_id": welder["id"], "quantity": 1},
        ],
    }]})
    upgraded = client.post(
        f"/lookahead/instances/{inst['id']}/upgrade").json()
    bom = upgraded["cards"][0]["resources"]
    welder_rows = [r for r in bom if r["resource_id"] == welder["id"]]
    # User's pre-existing welder row is preserved (status stays 'secured').
    assert len(welder_rows) == 1
    assert welder_rows[0]["status"] == "secured"


def test_upgrade_instance_is_noop_when_already_current(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    r = client.post(f"/lookahead/instances/{inst['id']}/upgrade")
    assert r.status_code == 200
    assert r.json()["template_version"] == 1


def test_template_task_carries_linked_procedure_doc_to_card(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload(tasks=[{
        "local_id": "x", "title": "Follow SOP",
        "offset_start_days": 0, "duration_shifts": 1,
        "linked_procedure_doc": "https://docs.example/sop-42",
    }])).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    assert inst["cards"][0]["linked_procedure_doc"] == "https://docs.example/sop-42"


# ── Cross-system linking (parsival#50) ────────────────────────────────────────

def _seed_item(item_id, project, title, timestamp="2026-04-15T08:00:00"):
    """Drop a minimal items row directly so the annotator has something to chew on."""
    db.upsert_item({
        "item_id": item_id, "source": "email", "title": title,
        "author": "", "timestamp": timestamp, "project_tag": project,
        "summary": f"{title} summary", "category": "task", "priority": "medium",
        "action_items": [], "goals": [], "key_dates": [],
        "information_items": [], "references": [],
    })


def test_annotate_card_persists_suggestions_from_llm(client):
    _wipe_lookahead()
    db.conn().execute("DELETE FROM items")
    _seed_item("em-1", "P905", "Panel install scheduling")
    _seed_item("em-2", "P905", "Totally unrelated coffee order")
    card = client.post("/lookahead/cards", json=_card_payload(
        title="Panel install", project="P905")).json()

    fake_response = '[{"item_id": "em-1", "reason": "scheduling thread"}]'
    with patch("app._llm.generate", return_value=fake_response):
        r = client.post(f"/lookahead/cards/{card['id']}/annotate")
    assert r.json()["created"] == 1

    suggestions = client.get(f"/lookahead/cards/{card['id']}/suggestions").json()
    assert len(suggestions) == 1
    assert suggestions[0]["target_id"] == "em-1"
    assert suggestions[0]["reason"] == "scheduling thread"
    assert suggestions[0]["target_title"] == "Panel install scheduling"


def test_annotate_skips_already_linked_items(client):
    _wipe_lookahead()
    db.conn().execute("DELETE FROM items")
    _seed_item("em-1", "P905", "Linked already")
    card = client.post("/lookahead/cards", json=_card_payload(
        title="X", project="P905",
        links=[{"type": "item", "id": "em-1"}])).json()

    with patch("app._llm.generate",
               return_value='[{"item_id": "em-1", "reason": "x"}]'):
        r = client.post(f"/lookahead/cards/{card['id']}/annotate")
    assert r.json()["created"] == 0


def test_annotate_is_idempotent_per_target(client):
    _wipe_lookahead()
    db.conn().execute("DELETE FROM items")
    _seed_item("em-1", "P905", "Something")
    card = client.post("/lookahead/cards", json=_card_payload(
        title="X", project="P905")).json()
    fake = '[{"item_id": "em-1", "reason": "match"}]'
    with patch("app._llm.generate", return_value=fake):
        client.post(f"/lookahead/cards/{card['id']}/annotate")
        r2 = client.post(f"/lookahead/cards/{card['id']}/annotate")
    assert r2.json()["created"] == 0
    assert len(client.get(f"/lookahead/cards/{card['id']}/suggestions").json()) == 1


def test_accept_suggestion_creates_card_link(client):
    _wipe_lookahead()
    db.conn().execute("DELETE FROM items")
    _seed_item("em-1", "P905", "Relevant thread")
    card = client.post("/lookahead/cards", json=_card_payload(
        title="X", project="P905")).json()
    with patch("app._llm.generate",
               return_value='[{"item_id": "em-1", "reason": "r"}]'):
        client.post(f"/lookahead/cards/{card['id']}/annotate")
    sugg = client.get(f"/lookahead/cards/{card['id']}/suggestions").json()[0]

    r = client.post(f"/lookahead/suggestions/{sugg['id']}/accept")
    assert r.json()["decision"] == "accepted"
    refreshed = client.get(f"/lookahead/cards/{card['id']}").json()
    assert {"type": "item", "id": "em-1"} in refreshed["links"]
    # Accepted suggestion no longer surfaces as pending.
    assert client.get(f"/lookahead/cards/{card['id']}/suggestions").json() == []


def test_reject_suggestion_does_not_create_link(client):
    _wipe_lookahead()
    db.conn().execute("DELETE FROM items")
    _seed_item("em-1", "P905", "Noise thread")
    card = client.post("/lookahead/cards", json=_card_payload(
        title="X", project="P905")).json()
    with patch("app._llm.generate",
               return_value='[{"item_id": "em-1", "reason": "r"}]'):
        client.post(f"/lookahead/cards/{card['id']}/annotate")
    sugg = client.get(f"/lookahead/cards/{card['id']}/suggestions").json()[0]
    client.post(f"/lookahead/suggestions/{sugg['id']}/reject")
    refreshed = client.get(f"/lookahead/cards/{card['id']}").json()
    user_links = [l for l in refreshed["links"] if l["type"] != "todo"]
    assert user_links == []


def test_annotate_project_fans_out_across_cards(client):
    _wipe_lookahead()
    db.conn().execute("DELETE FROM items")
    _seed_item("em-1", "P905", "Candidate")
    client.post("/lookahead/cards", json=_card_payload(title="A", project="P905"))
    client.post("/lookahead/cards", json=_card_payload(title="B", project="P905"))
    with patch("app._llm.generate",
               return_value='[{"item_id": "em-1", "reason": "r"}]'):
        r = client.post("/lookahead/annotate-project",
                        json={"project": "P905"})
    body = r.json()
    assert body["processed"] == 2
    assert body["new_suggestions"] == 2


def test_delete_instance_cascades_attached_cards(client):
    _wipe_lookahead()
    tpl = client.post("/lookahead/templates", json=_template_payload()).json()
    inst = client.post(f"/lookahead/templates/{tpl['id']}/instantiate",
                       json={"start_date": "2026-05-04",
                             "project_tag": "P905"}).json()
    card_ids = [c["id"] for c in inst["cards"]]
    client.delete(f"/lookahead/instances/{inst['id']}")
    for cid in card_ids:
        assert client.get(f"/lookahead/cards/{cid}").status_code == 404
    assert client.get(f"/lookahead/instances/{inst['id']}").status_code == 404


# ── Card ↔ todo round-trip (parsival#71) ──────────────────────────────────────

def _clear_todos():
    db.conn().execute("DELETE FROM todos")


def test_creating_card_creates_linked_todo(client):
    _wipe_lookahead()
    _clear_todos()
    card = client.post("/lookahead/cards",
                       json=_card_payload(title="Weld frame")).json()
    todo_id = db.get_card_todo_id(card["id"])
    assert todo_id is not None
    todo = db.get_todo_by_id(todo_id)
    assert todo["description"] == "Weld frame"
    assert todo["deadline"] == card["end_date"]
    assert todo["project_tag"] == "P905"
    assert todo["source"] == "lookahead"
    assert todo["done"] == 0


def test_completing_card_marks_linked_todo_done(client):
    _wipe_lookahead()
    _clear_todos()
    card = client.post("/lookahead/cards", json=_card_payload()).json()
    todo_id = db.get_card_todo_id(card["id"])
    client.patch(f"/lookahead/cards/{card['id']}", json={"status": "done"})
    todo = db.get_todo_by_id(todo_id)
    assert todo["done"] == 1
    assert todo["status"] == "done"


def test_completing_todo_marks_linked_card_done(client):
    _wipe_lookahead()
    _clear_todos()
    card = client.post("/lookahead/cards", json=_card_payload()).json()
    todo_id = db.get_card_todo_id(card["id"])
    r = client.patch(f"/todos/{todo_id}", json={"status": "done"})
    assert r.status_code == 200
    card_after = client.get(f"/lookahead/cards/{card['id']}").json()
    assert card_after["status"] == "done"


def test_reopening_todo_reopens_linked_card(client):
    _wipe_lookahead()
    _clear_todos()
    card = client.post("/lookahead/cards",
                       json=_card_payload(status="done")).json()
    todo_id = db.get_card_todo_id(card["id"])
    # Card was created done; its todo should be done too.
    assert db.get_todo_by_id(todo_id)["done"] == 1
    client.patch(f"/todos/{todo_id}", json={"status": "open"})
    card_after = client.get(f"/lookahead/cards/{card['id']}").json()
    assert card_after["status"] == "planned"


def test_deleting_card_deletes_linked_todo(client):
    _wipe_lookahead()
    _clear_todos()
    card = client.post("/lookahead/cards", json=_card_payload()).json()
    todo_id = db.get_card_todo_id(card["id"])
    client.delete(f"/lookahead/cards/{card['id']}")
    assert db.get_todo_by_id(todo_id) is None


def test_deleting_todo_unlinks_from_card(client):
    _wipe_lookahead()
    _clear_todos()
    card = client.post("/lookahead/cards", json=_card_payload()).json()
    todo_id = db.get_card_todo_id(card["id"])
    client.delete(f"/todos/{todo_id}")
    # Card survives; link row gone.
    assert client.get(f"/lookahead/cards/{card['id']}").status_code == 200
    assert db.get_card_todo_id(card["id"]) is None


def test_user_links_preserve_todo_link(client):
    _wipe_lookahead()
    _clear_todos()
    card = client.post("/lookahead/cards", json=_card_payload()).json()
    todo_id = db.get_card_todo_id(card["id"])
    # User edits the card's manual links — todo link must survive.
    client.patch(f"/lookahead/cards/{card['id']}",
                 json={"links": [{"type": "situation", "id": "sit-1"}]})
    assert db.get_card_todo_id(card["id"]) == todo_id


def test_backfill_creates_todos_for_orphan_cards():
    import app as _app
    _wipe_lookahead()
    db.conn().execute("DELETE FROM todos")
    # Raw-insert a card so no todo/link is created.
    db.upsert_lookahead_card({
        "id": "orphan-1", "title": "Legacy card", "project": "P905",
        "assignee": "Alice", "start_date": "2026-04-15",
        "start_shift_num": 1, "end_date": "2026-04-16",
        "end_shift_num": 1, "status": "planned",
    })
    # Reset the marker so the backfill can run again in this test run.
    db.conn().execute("DELETE FROM model_state WHERE key = ?",
                      ("lookahead_todo_backfill_v1",))
    _app._backfill_lookahead_todos()
    todo_id = db.get_card_todo_id("orphan-1")
    assert todo_id is not None
    assert db.get_todo_by_id(todo_id)["description"] == "Legacy card"
    # Second invocation is a no-op (marker set).
    before = db.get_todo_by_id(todo_id)["id"]
    _app._backfill_lookahead_todos()
    assert db.get_card_todo_id("orphan-1") == before
