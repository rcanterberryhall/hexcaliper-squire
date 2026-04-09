"""
seeder.py — Seed state machine for bootstrapping project config.

Implements the multi-stage seed workflow that bootstraps project and topic
configuration from an existing corpus using a map-reduce LLM pass:

  start()          — begin the seed job (POST /seed)
  update_context() — update context while waiting for ingest
  status()         — return current job state (GET /seed/status)
  cancel()         — signal the job to stop (POST /analysis/stop)
  apply()          — apply reviewed projects/topics (POST /seed/apply)
  run_scan()       — start a full connector scan (POST /seed/scan)
  skip_scan()      — skip to done without scanning (POST /seed/skip_scan)

Uses db.py directly for all persistence.  scan_state, run_scan_fn,
run_reanalyze_fn, and maybe_form_situation_fn are injected via init().
"""
import json
import threading

import requests as http_requests

import config
import db
import llm
import orchestrator

# ── Module-level references, set by init() ────────────────────────────────────

_scan_state:          dict = {}
_run_scan                   = None
_run_reanalyze              = None
_maybe_form_situation       = None

_seed_job: dict = {"status": "idle"}


def init(scan_state: dict, run_scan_fn, run_reanalyze_fn, maybe_form_situation_fn) -> None:
    """
    Inject shared state and callables from app.py.

    Must be called once at startup before any seed endpoints are invoked.
    """
    global _scan_state, _run_scan, _run_reanalyze, _maybe_form_situation
    _scan_state           = scan_state
    _run_scan             = run_scan_fn
    _run_reanalyze        = run_reanalyze_fn
    _maybe_form_situation = maybe_form_situation_fn


# ── LLM prompts ────────────────────────────────────────────────────────────────

MAP_PROMPT = """\
You are analyzing work items from {user_name}'s ops inbox.
{context_block}
{existing_projects_block}

Identify the major ongoing projects or workstreams from these items. A project is a \
sustained effort spanning multiple emails or days — not a single email or one-off task. \
Group related emails into the same project. Return at most 8 projects per batch. \
Passdown notes describe active operational handoffs — weight them heavily.

Keyword rules:
- Return 5-8 keywords per project
- Include project codes and numbers (e.g. "P905", "RV09", "P1304")
- Include related sub-codes or vehicle/unit identifiers that appear (e.g. "RV08", "RV15")
- Include technical terms, part names, and system names specific to the workstream
- Include key people or company names strongly associated with the project
- DO NOT use the full project name as a keyword
- DO NOT use generic words like "update", "implementation", "documentation", "logistics", "issue"

If the user already has projects configured, use their EXACT project names when the \
content matches. Only propose a new project name if no existing project fits.

Do NOT create projects for automated notifications (security digests, quarantine alerts, \
marketing emails, system notifications). Those are noise, not projects.

Items:
{items_block}

Respond ONLY with valid JSON — no markdown, no explanation:
{{
  "projects": [
    {{"name": "short project name", "keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"]}}
  ],
  "concerns": ["brief recurring concern phrase"]
}}
"""

REDUCE_PROMPT = """\
You are synthesizing project intelligence for {user_name}.
{context_block}
{existing_projects_block}

Below are theme extracts from {n_batches} batches covering {n_items} work items.

{themes_block}

Produce a final consolidated list of discovered projects. IMPORTANT RULES:

1. If the user already has projects configured, use their EXACT names. Merge any \
discovered themes into the matching existing project rather than creating a new one. \
For example, if the user has "Seatbelts upgrades" and the batches found "Seatbelt \
Restraint System", merge into the existing name "Seatbelts upgrades".

2. Only propose NEW projects for workstreams that genuinely do not match any existing \
project. Keep new additions to 3-5 max.

3. Every project MUST have 5-8 keywords. Include project codes, sub-codes, technical \
terms, part names, and key personnel. Remove generic words (implementation, \
documentation, logistics, update, issue).

4. Do NOT create projects for automated notifications, security digests, marketing \
emails, or system alerts. Those belong in noise filters.

5. Topics are PERSISTENT operational concerns spanning weeks or months — not individual \
tasks. Good: "procurement delays", "site access coordination". \
Bad: "availability for tomorrow", "transformer confirmation needed". \
Keep topics to 3-5 words max, at most 5 topics total.

Respond ONLY with valid JSON — no markdown, no explanation:
{{
  "projects": [
    {{"name": "canonical project name", "keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"]}}
  ],
  "topics": ["watch topic or recurring concern phrase"]
}}
"""


# ── Background job ─────────────────────────────────────────────────────────────

def _run_seed_job(context: str) -> None:
    """
    Background thread for POST /seed.  Implements the full bootstrap state machine:

    waiting_for_ingest → analyzing → review (thread exits; user applies)

    After apply, seed_apply() transitions to reanalyzing → scan_prompt → done.
    """
    import time
    global _seed_job
    try:
        user_name = config.USER_NAME or "the user"

        # ── State: waiting_for_ingest ─────────────────────────────────────────
        seen_items = False
        while True:
            with db.lock:
                item_count = db.count_items()
            pending = _scan_state.get("ingest_pending", 0)
            if item_count > 0:
                seen_items = True
            _seed_job.update({
                "state":          "waiting_for_ingest",
                "item_count":     item_count,
                "ingest_pending": pending,
                "progress":       (
                    f"{item_count} item{'s' if item_count != 1 else ''} received"
                    + (f", {pending} processing…" if pending else
                       " — ingest complete" if seen_items else "")
                ),
            })
            if pending == 0:
                break
            if _seed_job.get("cancelled"):
                _seed_job.update({"state": "idle", "status": "idle"})
                return
            time.sleep(3)

        # ── State: analyzing ──────────────────────────────────────────────────
        context = _seed_job.get("context") or context
        context_block = f"Context about {user_name}: {context}" if context else ""
        _seed_job.update({"state": "analyzing", "progress": "Starting analysis…"})

        # Build existing-projects block so the LLM merges into user's names
        existing_projects_block = ""
        if config.PROJECTS:
            lines = []
            for p in config.PROJECTS:
                name = p.get("name", "")
                desc = p.get("description", "")
                parent = p.get("parent", "")
                kw = list(p.get("keywords", [])) + list(p.get("learned_keywords", []))
                entry = name
                if parent:
                    entry += f" [sub-project of {parent}]"
                if desc:
                    entry += f" — {desc}"
                if kw:
                    entry += f" (keywords: {', '.join(kw[:10])})"
                lines.append(f"  - {entry}")
            existing_projects_block = (
                "\nThe user already has these projects configured — use their EXACT "
                "names when content matches:\n" + "\n".join(lines)
            )

        with db.lock:
            all_items = db.get_all_items()

        priority_rank = {"high": 3, "medium": 2, "low": 1}

        def _sort_key(a):
            is_pd = 1 if a.get("is_passdown") else 0
            pri   = priority_rank.get(a.get("priority", "low"), 1)
            return (is_pd, pri)

        all_items.sort(key=_sort_key, reverse=True)
        all_items = all_items[:120]

        passdown_count = sum(1 for a in all_items if a.get("is_passdown"))
        n_items        = len(all_items)

        batch_size = 15
        n_batches  = max(1, (n_items + batch_size - 1) // batch_size)

        print(f"[seed] analysing {n_items} items ({passdown_count} passdowns) in {n_batches} batches")
        _seed_job["progress"] = f"Analysing {n_items} items ({passdown_count} passdowns)…"

        # Map pass
        map_results  = []
        last_map_err = None
        for batch_num, batch_start in enumerate(range(0, n_items, batch_size), 1):
            if _seed_job.get("cancelled") or _scan_state.get("cancelled"):
                _seed_job.update({"state": "idle", "status": "idle", "progress": "Cancelled."})
                return
            _seed_job["progress"] = f"Map pass: batch {batch_num}/{n_batches}…"
            batch = all_items[batch_start:batch_start + batch_size]
            lines = []
            for a in batch:
                source   = a.get("source", "?")
                title    = a.get("title", "(no title)")
                summary  = (a.get("summary", "") or "")[:120]
                priority = a.get("priority", "low")
                is_pd    = a.get("is_passdown", False)
                suffix   = " [passdown]" if is_pd else ""
                lines.append(f"[{source}]{suffix} {title}: {summary} ({priority})")
            items_block = "\n".join(lines)

            prompt = MAP_PROMPT.format(
                user_name=user_name,
                context_block=context_block,
                existing_projects_block=existing_projects_block,
                items_block=items_block,
            )
            print(f"[seed] map batch {batch_num}/{n_batches}: {len(batch)} items")
            try:
                with orchestrator.get_sem():
                    text = llm.generate(
                        prompt, format="json", temperature=0.2,
                        num_predict=900, timeout=120,
                    )
                print(f"[seed] map batch {batch_num} raw response: {text!r}")
                data = json.loads(text or "{}")
                print(f"[seed] map batch {batch_num}: {len(data.get('projects', []))} projects, {len(data.get('concerns', []))} concerns")
                map_results.append(data)
            except Exception as e:
                last_map_err = str(e)
                print(f"[seed] map batch {batch_start} failed: {e}")
                continue

        print(f"[seed] map pass complete: {len(map_results)}/{n_batches} batches succeeded")
        if not map_results:
            err_detail = f" ({last_map_err})" if last_map_err else ""
            _seed_job.update({
                "state":          "error",
                "status":         "error",
                "progress":       f"All map batches failed{err_detail}",
                "projects":       [],
                "topics":         [],
                "item_count":     n_items,
                "passdown_count": passdown_count,
            })
            return

        # Reduce pass
        _seed_job["progress"] = f"Reduce pass: consolidating {len(map_results)} batches…"
        themes_parts = []
        for i, mr in enumerate(map_results):
            projects_str = json.dumps(mr.get("projects", []))
            concerns_str = json.dumps(mr.get("concerns", []))
            themes_parts.append(f"Batch {i + 1}:\n  projects: {projects_str}\n  concerns: {concerns_str}")
        themes_block = "\n\n".join(themes_parts)

        reduce_prompt = REDUCE_PROMPT.format(
            user_name=user_name,
            context_block=context_block,
            existing_projects_block=existing_projects_block,
            n_batches=len(map_results),
            n_items=n_items,
            themes_block=themes_block,
        )

        try:
            with orchestrator.get_sem():
                text = llm.generate(
                    reduce_prompt, format="json", temperature=0.2,
                    num_predict=1800, timeout=180,
                )
            print(f"[seed] reduce raw response: {text!r}")
            final    = json.loads(text or "{}")
            projects = final.get("projects", [])
            topics   = final.get("topics", [])
            print(f"[seed] reduce result: {len(projects)} projects, {len(topics)} topics")
        except Exception as e:
            print(f"[seed] reduce failed: {e}")
            projects   = []
            seen_names = set()
            topics     = []
            for mr in map_results:
                for p in mr.get("projects", []):
                    nm = p.get("name", "")
                    if nm and nm not in seen_names:
                        seen_names.add(nm)
                        projects.append(p)
                topics.extend(mr.get("concerns", []))
            topics = list(dict.fromkeys(topics))
            print(f"[seed] reduce fallback: {len(projects)} projects, {len(topics)} topics")

        print(f"[seed] final projects: {json.dumps(projects, indent=2)}")
        print(f"[seed] final topics: {topics}")
        _seed_job.update({
            "state":          "review",
            "status":         "running",
            "progress":       f"Analysis complete — {n_items} items reviewed.",
            "projects":       projects,
            "topics":         topics,
            "item_count":     n_items,
            "passdown_count": passdown_count,
        })

    except Exception as e:
        _seed_job.update({"state": "error", "status": "error", "progress": str(e)})


# ── Public interface ───────────────────────────────────────────────────────────

def start(context: str) -> dict:
    """
    Start the seed state machine.  Always succeeds immediately.
    Returns the current _seed_job state.
    """
    global _seed_job
    active_states = {"waiting_for_ingest", "analyzing", "reanalyzing", "scanning"}
    if _seed_job.get("state") in active_states:
        return _seed_job

    _seed_job = {
        "state":    "waiting_for_ingest",
        "status":   "running",
        "progress": "Waiting for ingest…",
        "context":  context,
        "item_count":     0,
        "ingest_pending": 0,
    }
    threading.Thread(target=_run_seed_job, args=(context,), daemon=True).start()
    return _seed_job


def update_context(context: str) -> None:
    """Update the user-provided context string while waiting for ingest."""
    _seed_job["context"] = context


def status() -> dict:
    """Return the current state of the background seed job."""
    return _seed_job


def cancel() -> None:
    """Signal the seed job to stop after the current step."""
    _seed_job["cancelled"] = True


def apply(body: dict, background_tasks) -> dict:
    """
    Apply the seed editor's confirmed projects and topics to settings.

    Steps:
    1. Merges new projects into config.PROJECTS (skipping duplicates).
    2. Merges new topics into config.FOCUS_TOPICS.
    3. Persists updated settings and calls config.apply_overrides.
    4. Optionally retags existing analyses against newly added projects.
    5. Background: embeds tagged items and sweeps situation formation.
    6. Starts _run_reanalyze in a daemon thread and monitors it.

    :param body: Dict with keys ``projects``, ``topics``, ``retag``.
    :param background_tasks: FastAPI BackgroundTasks runner.
    :return: ``{"ok": True, "projects_added": N, "projects_merged": M, "topics_added": T, "items_retagged": K}``
    """
    global _seed_job
    suggested_projects = body.get("projects", [])
    suggested_topics   = body.get("topics", [])
    retag              = body.get("retag", True)

    with db.lock:
        existing = db.get_settings()

    current_projects: list[dict] = existing.get("projects", list(config.PROJECTS))
    current_names = {p.get("name", "").lower(): i for i, p in enumerate(current_projects)}

    projects_added  = 0
    projects_merged = 0
    for sp in suggested_projects:
        name = sp.get("name", "").strip()
        if not name:
            continue
        name_lower = name.lower()

        # Check for exact match first
        if name_lower in current_names:
            # Merge: add any new keywords the seed found into the existing project
            idx = current_names[name_lower]
            existing_kw = set(
                k.lower() for k in
                current_projects[idx].get("keywords", [])
                + current_projects[idx].get("learned_keywords", [])
            )
            new_kw = [
                k for k in sp.get("keywords", [])
                if k and k.lower() not in existing_kw
            ]
            if new_kw:
                current_projects[idx].setdefault("keywords", []).extend(new_kw)
                projects_merged += 1
            # Fill in description if existing project has none and seed provides one
            if sp.get("description") and not current_projects[idx].get("description"):
                current_projects[idx]["description"] = sp["description"]
            # Fill in parent if existing project has none and seed provides one
            if sp.get("parent") and not current_projects[idx].get("parent"):
                current_projects[idx]["parent"] = sp["parent"]
            continue

        # New project — add with all fields from the seed editor
        current_projects.append({
            "name":             name,
            "parent":           sp.get("parent", ""),
            "description":      sp.get("description", ""),
            "keywords":         sp.get("keywords", []),
            "senders":          sp.get("senders", []),
            "channels":         [],
            "learned_keywords": [],
            "learned_senders":  [],
        })
        current_names[name_lower] = len(current_projects) - 1
        projects_added += 1

    existing_ft_raw = existing.get("focus_topics", ", ".join(config.FOCUS_TOPICS))
    existing_topics = [t.strip() for t in existing_ft_raw.split(",") if t.strip()]
    existing_topics_lower = {t.lower() for t in existing_topics}
    topics_added = 0
    for t in suggested_topics:
        t = t.strip()
        if t and t.lower() not in existing_topics_lower:
            existing_topics.append(t)
            existing_topics_lower.add(t.lower())
            topics_added += 1
    new_focus_topics = ", ".join(existing_topics)

    existing["projects"]     = current_projects
    existing["focus_topics"] = new_focus_topics
    with db.lock:
        db.save_settings(existing)
    config.apply_overrides(existing)

    items_retagged = 0
    if retag and projects_added > 0:
        new_projects = current_projects[-projects_added:]
        with db.lock:
            all_items = db.get_all_items()

        updates = []  # (item_id, [matching_project_names])
        for item in all_items:
            existing_tags = db.parse_project_tags(item.get("project_tag"))
            text = " ".join([
                item.get("title", ""),
                item.get("body_preview", ""),
                item.get("summary", ""),
            ]).lower()
            matched = []
            for proj in new_projects:
                if proj["name"] in existing_tags:
                    continue  # already tagged
                kws = proj.get("keywords", []) + proj.get("learned_keywords", [])
                if any(kw.lower() in text for kw in kws if kw):
                    matched.append(proj["name"])
            if matched:
                new_tags = existing_tags + matched
                updates.append((item.get("item_id"), db.serialize_project_tags(new_tags)))
                items_retagged += 1

        if updates:
            with db.lock:
                for item_id, tag_val in updates:
                    db.update_item(item_id, {"project_tag": tag_val})

    def _seed_embed_and_correlate() -> None:
        """
        Background task run after apply() to warm the embedding and
        situation layers from the existing corpus.

        Waits for reanalysis to complete first so embeddings and
        situations are built from fully updated item data (with correct
        project tags, categories, and priorities from the LLM).
        """
        import time as _time
        # Give the reanalysis thread a moment to start and set running=True
        _time.sleep(3)
        while _scan_state.get("running"):
            _time.sleep(3)
        print("[seed] reanalysis complete — starting embedding sweep...")
        try:
            from embedder import embed, update_project
            with db.lock:
                all_items = db.get_all_items()
            for item in all_items:
                tags = db.parse_project_tags(item.get("project_tag"))
                if not tags:
                    continue
                text = " ".join(filter(None, [
                    item.get("title", ""),
                    item.get("body_preview", ""),
                    item.get("summary", ""),
                ]))
                if not text.strip():
                    continue
                try:
                    vector = embed(text)
                    if vector:
                        for tag in tags:
                            update_project(
                                project_name = tag,
                                item_id      = item.get("item_id", ""),
                                vector       = vector,
                                category     = item.get("category", "fyi"),
                                hierarchy    = item.get("hierarchy", "general"),
                                source       = item.get("source", ""),
                                priority     = item.get("priority", "medium"),
                            )
                except Exception as e:
                    print(f"[seed] embed {item.get('item_id')}: {e}")
            print("[seed] embedding sweep complete")
        except Exception as e:
            print(f"[seed] embedding sweep failed: {e}")

        print("[seed] starting situation formation sweep...")
        try:
            with db.lock:
                all_items = db.get_all_items()
            item_ids = [item.get("item_id") for item in all_items if item.get("item_id")]
            with db.lock:
                _scan_state["situations_pending"] += len(item_ids)
            for iid in item_ids:
                try:
                    _maybe_form_situation(iid)
                except Exception as e:
                    print(f"[seed] situation sweep {iid}: {e}")
                finally:
                    with db.lock:
                        _scan_state["situations_pending"] = max(0, _scan_state["situations_pending"] - 1)
            print(f"[seed] situation sweep complete ({len(item_ids)} items)")
        except Exception as e:
            print(f"[seed] situation sweep failed: {e}")

    background_tasks.add_task(_seed_embed_and_correlate)

    _seed_job.update({
        "state":    "reanalyzing",
        "status":   "running",
        "progress": "Re-analyzing all items with new project config…",
    })
    threading.Thread(target=_run_reanalyze, daemon=True).start()

    def _monitor_reanalyze() -> None:
        import time
        time.sleep(1)
        while _scan_state.get("running"):
            _seed_job["progress"] = _scan_state.get("message", "Re-analyzing…")
            time.sleep(2)
        _seed_job.update({
            "state":    "scan_prompt",
            "status":   "running",
            "progress": "Re-analysis complete.",
        })

    threading.Thread(target=_monitor_reanalyze, daemon=True).start()

    return {
        "ok":              True,
        "projects_added":  projects_added,
        "projects_merged": projects_merged,
        "topics_added":    topics_added,
        "items_retagged":  items_retagged,
    }


def run_scan(scan_state: dict) -> dict:
    """
    Transition the seed state machine to scanning and start a full connector scan.

    :param scan_state: The shared scan_state dict from app.py.
    :return: ``{"ok": True}``
    :raises fastapi.HTTPException 409: If a scan is already running.
    """
    from fastapi import HTTPException
    global _seed_job
    if scan_state["running"]:
        raise HTTPException(status_code=409, detail="A scan is already running.")
    _seed_job.update({"state": "scanning", "status": "running", "progress": "Starting scan…"})
    sources = ["slack", "github", "jira", "outlook", "teams"]
    threading.Thread(target=_run_scan, args=(sources,), daemon=True).start()

    def _monitor_scan() -> None:
        import time
        time.sleep(1)
        while scan_state.get("running"):
            _seed_job["progress"] = scan_state.get("message", "Scanning…")
            time.sleep(2)
        _seed_job.update({"state": "done", "status": "done", "progress": "Setup complete."})

    threading.Thread(target=_monitor_scan, daemon=True).start()
    return {"ok": True}


def skip_scan() -> dict:
    """
    Transition the seed state machine from scan_prompt to done without scanning.

    :return: ``{"ok": True}``
    """
    global _seed_job
    _seed_job.update({"state": "done", "status": "done", "progress": "Setup complete."})
    return {"ok": True}
