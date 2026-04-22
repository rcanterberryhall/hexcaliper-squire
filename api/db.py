"""
db.py — SQLite database layer for Squire.

Replaces TinyDB with SQLite.  All document and graph data lives in a single
SQLite file at ``config.DB_PATH``.  WAL mode allows concurrent reads while
serialising writes through the module-level ``lock`` (a threading.Lock).

Document tables mirror the old TinyDB tables:
  items       — analysis records (was "analyses")
  todos       — action-item rows
  intel       — information-item rows
  situations  — cross-source situation clusters
  settings    — single-row config blob
  scan_logs   — scan run history
  embeddings  — sentence-embedding centroids per project

Graph tables (new):
  nodes  — typed entity registry
  edges  — typed, weighted, timestamped relationships

Public interface
---------------
Each table has dedicated helper functions.  Callers that need an atomic
multi-step operation should acquire ``db.lock`` themselves:

    with db.lock:
        db.upsert_item(data)
        db.insert_todo(todo_data)

``db.lock`` is a re-entrant ``threading.RLock`` so a thread that already
holds the lock can call any helper without self-deadlocking, even if the
helper itself wraps its work in ``with db.lock:``.  This is defence in
depth — most helpers do not acquire the lock internally — but it means
the convention above is safe rather than load-bearing.

The ``conn()`` function returns the thread-shared connection; callers can
run raw SQL when the helpers do not cover a use-case.
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import config

# ── Project-tag helpers (multi-tag support) ───────────────────────────────────
#
# project_tag is stored as a JSON array string: '["P905","Seatbelts upgrades"]'
# For backward compat, a bare string like "P905" is treated as '["P905"]'.

def parse_project_tags(val) -> list[str]:
    """Parse a project_tag column value into a list of project names.

    Handles: None → [], bare string → [string], JSON array string → list.
    """
    if not val:
        return []
    if isinstance(val, list):
        return [t for t in val if t]
    val = val.strip()
    if val.startswith("["):
        try:
            tags = json.loads(val)
            return [t for t in tags if isinstance(t, str) and t]
        except (json.JSONDecodeError, TypeError):
            return [val] if val else []
    return [val]


def serialize_project_tags(tags) -> str | None:
    """Serialize a list of project names to a JSON array string for storage.

    Returns None for empty lists (column stays NULL for untagged items).
    """
    if not tags:
        return None
    if isinstance(tags, str):
        tags = parse_project_tags(tags)
    tags = [t for t in tags if t]
    if not tags:
        return None
    if len(tags) == 1:
        return tags[0]  # keep single tags as plain strings for readability
    return json.dumps(tags)


def item_has_project(item: dict, project: str) -> bool:
    """Check whether an item record is tagged to a given project."""
    return project in parse_project_tags(item.get("project_tag"))


def item_has_any_project(item: dict) -> bool:
    """Check whether an item has any project tag at all."""
    return bool(parse_project_tags(item.get("project_tag")))


# ── Module-level state ─────────────────────────────────────────────────────────

lock = threading.RLock()         # re-entrant: callers may nest without self-deadlock
_conn: Optional[sqlite3.Connection] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Connection & schema ────────────────────────────────────────────────────────

def conn() -> sqlite3.Connection:
    """Return the shared SQLite connection, creating it on first call."""
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
        _conn = sqlite3.connect(
            config.DB_PATH,
            check_same_thread=False,
            isolation_level=None,   # autocommit; we manage transactions manually
        )
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _create_schema(_conn)
        _migrate_schema(_conn)
    return _conn


def backfill_manual_todo_items() -> int:
    """Create placeholder items rows for manual todos that predate the
    synthesized-item model (is_manual=1, item_id IS NULL).

    Each orphan todo gets item_id='manual_<todo_id>' on both the todo and
    a synthesized items row whose title/body seed from the todo description
    so the detail panel has something to render.

    Idempotent: runs only against todos still missing item_id.

    Returns the number of rows backfilled.
    """
    c = conn()
    orphans = c.execute(
        "SELECT id, description, priority, project_tag, created_at "
        "FROM todos WHERE is_manual = 1 AND (item_id IS NULL OR item_id = '')"
    ).fetchall()
    for row in orphans:
        new_iid = f"manual_{row['id']}"
        desc    = row["description"] or ""
        upsert_item({
            "item_id":      new_iid,
            "source":       "manual",
            "direction":    "received",
            "title":        desc[:200],
            "author":       "",
            "timestamp":    row["created_at"] or _now_iso(),
            "url":          "",
            "has_action":   1,
            "priority":     row["priority"] or "medium",
            "category":     "task",
            "summary":      "",
            "action_items": "[]",
            "hierarchy":    "general",
            "project_tag":  row["project_tag"],
            "goals":        "[]",
            "key_dates":    "[]",
            "information_items": "[]",
            "body_preview": "",
            "references":   "[]",
        })
        c.execute("UPDATE todos SET item_id = ? WHERE id = ?", (new_iid, row["id"]))
    return len(orphans)


def _migrate_schema(c: sqlite3.Connection) -> None:
    """Apply incremental schema migrations that cannot use CREATE IF NOT EXISTS."""
    cols = {row[1] for row in c.execute("PRAGMA table_info(items)").fetchall()}
    if "batch_job_id" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN batch_job_id TEXT")
    if "user_edited_fields" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN user_edited_fields TEXT NOT NULL DEFAULT '[]'")

    sit_cols = {row[1] for row in c.execute("PRAGMA table_info(situations)").fetchall()}
    if "lifecycle_status" not in sit_cols:
        c.execute("ALTER TABLE situations ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'new'")
        # Migrate dismissed=1 rows to lifecycle_status='dismissed'
        c.execute("UPDATE situations SET lifecycle_status='dismissed' WHERE dismissed=1")
    if "follow_up_date" not in sit_cols:
        c.execute("ALTER TABLE situations ADD COLUMN follow_up_date TEXT")
    if "notes" not in sit_cols:
        c.execute("ALTER TABLE situations ADD COLUMN notes TEXT NOT NULL DEFAULT ''")

    # Contacts: provenance + manual-edit tracking for the signature parser
    # (squire#31).  Each editable field gets a *_source column tagging its
    # origin: 'header' (scraped from To/CC/author), 'signature' (parsed from
    # email body), or 'manual' (user typed it in the UI).  Fields the user
    # has touched are also recorded in manually_edited_fields so the parser
    # can never overwrite them.  signature_confidence stores per-field
    # confidence scores (0..1) so the UI can show how sure the parser was.
    contact_cols = {row[1] for row in c.execute("PRAGMA table_info(contacts)").fetchall()}
    for col in ("name_source", "phone_source", "employer_source",
                "title_source", "address_source"):
        if col not in contact_cols:
            c.execute(
                f"ALTER TABLE contacts ADD COLUMN {col} TEXT NOT NULL DEFAULT 'header'"
            )
    if "manually_edited_fields" not in contact_cols:
        c.execute(
            "ALTER TABLE contacts ADD COLUMN manually_edited_fields TEXT NOT NULL DEFAULT '[]'"
        )
    if "signature_confidence" not in contact_cols:
        c.execute(
            "ALTER TABLE contacts ADD COLUMN signature_confidence TEXT NOT NULL DEFAULT '{}'"
        )

    # Look-ahead cards: procedure doc URL copied from template tasks on
    # instantiation (parsival#50).  Optional on manually-authored cards.
    card_cols = {row[1] for row in c.execute("PRAGMA table_info(lookahead_cards)").fetchall()}
    if "linked_procedure_doc" not in card_cols:
        c.execute("ALTER TABLE lookahead_cards ADD COLUMN linked_procedure_doc TEXT NOT NULL DEFAULT ''")
    # Per-card workweek mask (parsival#73). Empty string = all seven days active
    # (default). Non-empty mask uses the same comma-separated DOW format as
    # project_shifts.days (e.g. "M,T,W,Th,F" for a Mon-Fri work week). Drives
    # both board rendering (off-days get a diagonal-hatch overlay) and
    # instantiation/reschedule math (offsets count only work days).
    if "work_days" not in card_cols:
        c.execute("ALTER TABLE lookahead_cards ADD COLUMN work_days TEXT NOT NULL DEFAULT ''")
    tpl_task_cols = {row[1] for row in c.execute("PRAGMA table_info(lookahead_template_tasks)").fetchall()}
    if tpl_task_cols and "work_days" not in tpl_task_cols:
        c.execute("ALTER TABLE lookahead_template_tasks ADD COLUMN work_days TEXT NOT NULL DEFAULT ''")

    # Suggestions pool for cross-system LLM linking (parsival#50).  Rows
    # start pending and become concrete card_links once the user accepts.
    c.execute("""
        CREATE TABLE IF NOT EXISTS lookahead_card_link_suggestions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id    TEXT    NOT NULL,
            link_type  TEXT    NOT NULL,
            target_id  TEXT    NOT NULL,
            reason     TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL DEFAULT '',
            decision   TEXT    DEFAULT NULL,
            decided_at TEXT,
            FOREIGN KEY (card_id) REFERENCES lookahead_cards(id) ON DELETE CASCADE
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_la_link_sugg_card ON lookahead_card_link_suggestions(card_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_la_link_sugg_decision ON lookahead_card_link_suggestions(decision)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS situation_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            situation_id TEXT    NOT NULL,
            from_status  TEXT,
            to_status    TEXT    NOT NULL,
            timestamp    TEXT    NOT NULL,
            note         TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS user_actions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     TEXT    NOT NULL,
            action_type TEXT    NOT NULL,
            timestamp   TEXT    NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS model_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '{}'
        )
    """)

    # Slack scan dedup (parsival#69): record every message (team, channel, ts)
    # once we've surfaced it, so subsequent scans skip messages the user has
    # already seen. Without this, the channel/DM path emitted a fresh item
    # every scan (keyed by the latest msg ts), regenerating the same todos.
    c.execute("""
        CREATE TABLE IF NOT EXISTS slack_seen_messages (
            team       TEXT NOT NULL DEFAULT '',
            channel_id TEXT NOT NULL,
            ts         TEXT NOT NULL,
            seen_at    TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (team, channel_id, ts)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_slack_seen_chan ON slack_seen_messages(channel_id)")

    # Backfill synthesized items rows for legacy manual todos (issue #85).
    # Running inside _migrate_schema ensures every live DB catches up exactly
    # once at next startup; subsequent runs are no-ops because orphans have
    # already been linked.
    backfill_manual_todo_items()


def _create_schema(c: sqlite3.Connection) -> None:
    """Create all tables and indexes if they do not already exist."""
    c.executescript("""
    -- ── Document tables ──────────────────────────────────────────────────────

    CREATE TABLE IF NOT EXISTS items (
        item_id          TEXT    PRIMARY KEY,
        source           TEXT    NOT NULL DEFAULT '',
        direction        TEXT    NOT NULL DEFAULT 'received',
        title            TEXT    NOT NULL DEFAULT '',
        author           TEXT    NOT NULL DEFAULT '',
        timestamp        TEXT    NOT NULL DEFAULT '',
        url              TEXT    NOT NULL DEFAULT '',
        has_action       INTEGER NOT NULL DEFAULT 0,
        priority         TEXT    DEFAULT 'low',
        category         TEXT    DEFAULT 'fyi',
        task_type        TEXT,
        summary          TEXT    NOT NULL DEFAULT '',
        user_summary     TEXT,
        urgency          TEXT,
        action_items     TEXT    NOT NULL DEFAULT '[]',
        hierarchy        TEXT    NOT NULL DEFAULT 'general',
        is_passdown      INTEGER NOT NULL DEFAULT 0,
        project_tag      TEXT,
        conversation_id  TEXT,
        conversation_topic TEXT,
        goals            TEXT    NOT NULL DEFAULT '[]',
        key_dates        TEXT    NOT NULL DEFAULT '[]',
        information_items TEXT   NOT NULL DEFAULT '[]',
        body_preview     TEXT    NOT NULL DEFAULT '',
        to_field         TEXT    NOT NULL DEFAULT '',
        cc_field         TEXT    NOT NULL DEFAULT '',
        is_replied       INTEGER NOT NULL DEFAULT 0,
        replied_at       TEXT,
        processed_at     TEXT,
        situation_id     TEXT,
        "references"     TEXT    NOT NULL DEFAULT '[]'
    );

    CREATE INDEX IF NOT EXISTS idx_items_timestamp    ON items(timestamp);
    CREATE INDEX IF NOT EXISTS idx_items_category     ON items(category);
    CREATE INDEX IF NOT EXISTS idx_items_project      ON items(project_tag);
    CREATE INDEX IF NOT EXISTS idx_items_conversation ON items(conversation_id);
    CREATE INDEX IF NOT EXISTS idx_items_situation    ON items(situation_id);
    CREATE INDEX IF NOT EXISTS idx_items_source       ON items(source, direction);

    CREATE TABLE IF NOT EXISTS todos (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id          TEXT,
        source           TEXT    NOT NULL DEFAULT '',
        title            TEXT    NOT NULL DEFAULT '',
        url              TEXT    NOT NULL DEFAULT '',
        description      TEXT    NOT NULL DEFAULT '',
        user_edited_text TEXT,
        deadline         TEXT,
        owner            TEXT    NOT NULL DEFAULT '',
        priority         TEXT    NOT NULL DEFAULT 'medium',
        done             INTEGER NOT NULL DEFAULT 0,
        status           TEXT    NOT NULL DEFAULT 'open',
        assigned_to      TEXT,
        is_manual        INTEGER NOT NULL DEFAULT 0,
        project_tag      TEXT,
        created_at       TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_todos_item     ON todos(item_id);
    CREATE INDEX IF NOT EXISTS idx_todos_status   ON todos(done, status);
    CREATE INDEX IF NOT EXISTS idx_todos_manual   ON todos(is_manual);
    CREATE INDEX IF NOT EXISTS idx_todos_project  ON todos(project_tag);

    CREATE TABLE IF NOT EXISTS intel (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id     TEXT,
        source      TEXT    NOT NULL DEFAULT '',
        title       TEXT    NOT NULL DEFAULT '',
        url         TEXT    NOT NULL DEFAULT '',
        fact        TEXT    NOT NULL DEFAULT '',
        relevance   TEXT    NOT NULL DEFAULT '',
        project_tag TEXT,
        priority    TEXT    NOT NULL DEFAULT 'medium',
        timestamp   TEXT,
        dismissed   INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_intel_item      ON intel(item_id);
    CREATE INDEX IF NOT EXISTS idx_intel_dismissed ON intel(dismissed);
    CREATE INDEX IF NOT EXISTS idx_intel_project   ON intel(project_tag);

    CREATE TABLE IF NOT EXISTS situations (
        situation_id     TEXT    PRIMARY KEY,
        title            TEXT    NOT NULL DEFAULT '',
        summary          TEXT    NOT NULL DEFAULT '',
        status           TEXT    NOT NULL DEFAULT 'in_progress',
        item_ids         TEXT    NOT NULL DEFAULT '[]',
        sources          TEXT    NOT NULL DEFAULT '[]',
        project_tag      TEXT,
        score            REAL    NOT NULL DEFAULT 0.0,
        priority         TEXT    NOT NULL DEFAULT 'medium',
        open_actions     TEXT    NOT NULL DEFAULT '[]',
        "references"     TEXT    NOT NULL DEFAULT '[]',
        key_context      TEXT,
        last_updated     TEXT,
        created_at       TEXT,
        score_updated_at TEXT,
        dismissed        INTEGER NOT NULL DEFAULT 0,
        dismiss_reason   TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_sit_score     ON situations(score DESC);
    CREATE INDEX IF NOT EXISTS idx_sit_dismissed ON situations(dismissed);
    CREATE INDEX IF NOT EXISTS idx_sit_project   ON situations(project_tag);

    CREATE TABLE IF NOT EXISTS briefings (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        generated_at TEXT    NOT NULL,
        content      TEXT    NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS settings (
        id   INTEGER PRIMARY KEY DEFAULT 1,
        data TEXT    NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS scan_logs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at    TEXT,
        finished_at   TEXT,
        sources       TEXT,
        items_scanned INTEGER DEFAULT 0,
        actions_found INTEGER DEFAULT 0,
        status        TEXT    DEFAULT 'completed'
    );

    CREATE TABLE IF NOT EXISTS embeddings (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project         TEXT    NOT NULL UNIQUE,
        items           TEXT    NOT NULL DEFAULT '[]',
        centroids       TEXT    NOT NULL DEFAULT '{}',
        centroid_counts TEXT    NOT NULL DEFAULT '{}'
    );

    -- ── Graph tables ──────────────────────────────────────────────────────────

    CREATE TABLE IF NOT EXISTS nodes (
        node_id    TEXT PRIMARY KEY,
        node_type  TEXT NOT NULL,
        label      TEXT NOT NULL DEFAULT '',
        properties TEXT NOT NULL DEFAULT '{}'
    );

    CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);

    CREATE TABLE IF NOT EXISTS edges (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        src_id     TEXT    NOT NULL,
        dst_id     TEXT    NOT NULL,
        edge_type  TEXT    NOT NULL,
        weight     REAL    NOT NULL DEFAULT 1.0,
        created_at TEXT    NOT NULL,
        properties TEXT    NOT NULL DEFAULT '{}'
    );

    CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src_id, edge_type);
    CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst_id, edge_type);
    CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_unique ON edges(src_id, dst_id, edge_type);

    -- ── Contacts tables ──────────────────────────────────────────────────────
    --
    -- Identity is a stable serial integer (contact_id), NOT an email or any
    -- other field that can change when a person switches jobs or providers.
    -- Emails live in a separate join table so a contact can carry multiple
    -- addresses across employers without losing history.

    CREATE TABLE IF NOT EXISTS contacts (
        contact_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        name             TEXT    NOT NULL DEFAULT '',
        phone            TEXT    NOT NULL DEFAULT '',
        employer         TEXT    NOT NULL DEFAULT '',
        title            TEXT    NOT NULL DEFAULT '',
        employer_address TEXT    NOT NULL DEFAULT '',
        notes            TEXT    NOT NULL DEFAULT '',
        first_seen       TEXT,
        last_seen        TEXT,
        source_count     INTEGER NOT NULL DEFAULT 0,
        last_item_id     TEXT,
        is_manual        INTEGER NOT NULL DEFAULT 0,
        -- Provenance per editable field: 'header' | 'signature' | 'manual'.
        -- Defaults to 'header' because that is the only source that existed
        -- before squire#31.  See _migrate_schema for the matching ALTERs.
        name_source      TEXT    NOT NULL DEFAULT 'header',
        phone_source     TEXT    NOT NULL DEFAULT 'header',
        employer_source  TEXT    NOT NULL DEFAULT 'header',
        title_source     TEXT    NOT NULL DEFAULT 'header',
        address_source   TEXT    NOT NULL DEFAULT 'header',
        -- JSON array of field names the user has manually edited.  The
        -- signature parser must never overwrite anything in this list.
        manually_edited_fields TEXT NOT NULL DEFAULT '[]',
        -- JSON object {field_name: 0..1} of confidence scores from the most
        -- recent signature-parser run.  Empty when no signature has been
        -- parsed yet.
        signature_confidence   TEXT NOT NULL DEFAULT '{}',
        created_at       TEXT,
        updated_at       TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_contacts_name      ON contacts(name);
    CREATE INDEX IF NOT EXISTS idx_contacts_employer  ON contacts(employer);
    CREATE INDEX IF NOT EXISTS idx_contacts_last_seen ON contacts(last_seen DESC);

    CREATE TABLE IF NOT EXISTS contact_emails (
        contact_id INTEGER NOT NULL,
        email      TEXT    NOT NULL,
        is_primary INTEGER NOT NULL DEFAULT 0,
        added_at   TEXT,
        PRIMARY KEY (contact_id, email),
        FOREIGN KEY (contact_id) REFERENCES contacts(contact_id) ON DELETE CASCADE
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_emails_email ON contact_emails(email);
    CREATE INDEX IF NOT EXISTS idx_contact_emails_contact ON contact_emails(contact_id);

    -- ── Look-ahead (parsival#48) ──────────────────────────────────────────────
    --
    -- Manually planned work on a 14-day rolling board, grouped by project.
    -- Cards have UUID PKs so they can be linked from other systems without
    -- leaking autoincrement counters.  Dates are YYYY-MM-DD strings, times
    -- HH:MM, shift-day lists are comma-separated (M,T,W,Th,F,Sa,Su) — all
    -- human-readable, no bitmasks or epoch ints.

    CREATE TABLE IF NOT EXISTS lookahead_cards (
        id                     TEXT    PRIMARY KEY,
        title                  TEXT    NOT NULL DEFAULT '',
        project                TEXT    NOT NULL DEFAULT '',
        assignee               TEXT    NOT NULL DEFAULT '',
        start_date             TEXT    NOT NULL DEFAULT '',
        start_shift_num        INTEGER NOT NULL DEFAULT 1,
        end_date               TEXT    NOT NULL DEFAULT '',
        end_shift_num          INTEGER NOT NULL DEFAULT 1,
        status                 TEXT    NOT NULL DEFAULT 'planned',
        notes                  TEXT    NOT NULL DEFAULT '',
        work_days              TEXT    NOT NULL DEFAULT '',
        linked_procedure_doc   TEXT    NOT NULL DEFAULT '',
        template_instance_id   TEXT,
        template_task_local_id TEXT,
        created_at             TEXT    NOT NULL DEFAULT '',
        updated_at             TEXT    NOT NULL DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_la_cards_project  ON lookahead_cards(project);
    CREATE INDEX IF NOT EXISTS idx_la_cards_dates    ON lookahead_cards(start_date, end_date);
    CREATE INDEX IF NOT EXISTS idx_la_cards_assignee ON lookahead_cards(assignee);
    CREATE INDEX IF NOT EXISTS idx_la_cards_status   ON lookahead_cards(status);

    CREATE TABLE IF NOT EXISTS lookahead_card_deps (
        card_id       TEXT NOT NULL,
        depends_on_id TEXT NOT NULL,
        PRIMARY KEY (card_id, depends_on_id),
        FOREIGN KEY (card_id)       REFERENCES lookahead_cards(id) ON DELETE CASCADE,
        FOREIGN KEY (depends_on_id) REFERENCES lookahead_cards(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_la_deps_card ON lookahead_card_deps(card_id);
    CREATE INDEX IF NOT EXISTS idx_la_deps_dep  ON lookahead_card_deps(depends_on_id);

    CREATE TABLE IF NOT EXISTS lookahead_card_links (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        card_id   TEXT    NOT NULL,
        link_type TEXT    NOT NULL,
        target_id TEXT    NOT NULL,
        FOREIGN KEY (card_id) REFERENCES lookahead_cards(id) ON DELETE CASCADE
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_la_links_unique ON lookahead_card_links(card_id, link_type, target_id);
    CREATE INDEX IF NOT EXISTS idx_la_links_card          ON lookahead_card_links(card_id);

    CREATE TABLE IF NOT EXISTS lookahead_resources (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL DEFAULT '',
        type       TEXT    NOT NULL DEFAULT 'person',
        notes      TEXT    NOT NULL DEFAULT '',
        created_at TEXT    NOT NULL DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_la_resources_type ON lookahead_resources(type);
    CREATE INDEX IF NOT EXISTS idx_la_resources_name ON lookahead_resources(name);

    CREATE TABLE IF NOT EXISTS lookahead_card_resources (
        card_id     TEXT    NOT NULL,
        resource_id INTEGER NOT NULL,
        quantity    REAL    NOT NULL DEFAULT 1,
        status      TEXT    NOT NULL DEFAULT 'needed',
        PRIMARY KEY (card_id, resource_id),
        FOREIGN KEY (card_id)     REFERENCES lookahead_cards(id)     ON DELETE CASCADE,
        FOREIGN KEY (resource_id) REFERENCES lookahead_resources(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_la_card_res_card ON lookahead_card_resources(card_id);
    CREATE INDEX IF NOT EXISTS idx_la_card_res_res  ON lookahead_card_resources(resource_id);

    CREATE TABLE IF NOT EXISTS project_shifts (
        project_tag TEXT    NOT NULL,
        shift_num   INTEGER NOT NULL,
        label       TEXT    NOT NULL DEFAULT '',
        start_time  TEXT    NOT NULL DEFAULT '',
        end_time    TEXT    NOT NULL DEFAULT '',
        days        TEXT    NOT NULL DEFAULT '',
        PRIMARY KEY (project_tag, shift_num)
    );

    -- ── Look-ahead templates (parsival#49) ───────────────────────────────────
    --
    -- Templates describe a repeatable piece of work as a tiny graph of tasks
    -- with relative offsets, duration, dependencies and resource requirements.
    -- Instantiating a template with a ``start_date`` materialises cards on the
    -- look-ahead board; their ``template_instance_id`` + ``template_task_local_id``
    -- fields tie them back to the originating instance so a future reschedule
    -- can move them all by the same delta.

    CREATE TABLE IF NOT EXISTS lookahead_templates (
        id                  TEXT    PRIMARY KEY,
        name                TEXT    NOT NULL DEFAULT '',
        description         TEXT    NOT NULL DEFAULT '',
        owner               TEXT    NOT NULL DEFAULT '',
        version             INTEGER NOT NULL DEFAULT 1,
        duration_unit       TEXT    NOT NULL DEFAULT 'calendar_days',
        default_project_tag TEXT    NOT NULL DEFAULT '',
        created_at          TEXT    NOT NULL DEFAULT '',
        updated_at          TEXT    NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS lookahead_template_tasks (
        template_id          TEXT    NOT NULL,
        local_id             TEXT    NOT NULL,
        title                TEXT    NOT NULL DEFAULT '',
        offset_start_days    INTEGER NOT NULL DEFAULT 0,
        offset_start_shift   INTEGER NOT NULL DEFAULT 1,
        duration_shifts      INTEGER NOT NULL DEFAULT 1,
        shift_preference     INTEGER NOT NULL DEFAULT 0,
        work_days            TEXT    NOT NULL DEFAULT '',
        linked_procedure_doc TEXT    NOT NULL DEFAULT '',
        PRIMARY KEY (template_id, local_id),
        FOREIGN KEY (template_id) REFERENCES lookahead_templates(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS lookahead_template_task_deps (
        template_id         TEXT NOT NULL,
        task_local_id       TEXT NOT NULL,
        depends_on_local_id TEXT NOT NULL,
        PRIMARY KEY (template_id, task_local_id, depends_on_local_id),
        FOREIGN KEY (template_id) REFERENCES lookahead_templates(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS lookahead_template_task_resources (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        template_id       TEXT    NOT NULL,
        task_local_id     TEXT    NOT NULL,
        resource_type     TEXT    NOT NULL DEFAULT '',
        role              TEXT    NOT NULL DEFAULT '',
        named_resource_id INTEGER,
        quantity          REAL    NOT NULL DEFAULT 1,
        FOREIGN KEY (template_id) REFERENCES lookahead_templates(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_la_tpl_task_res_task
        ON lookahead_template_task_resources(template_id, task_local_id);

    CREATE TABLE IF NOT EXISTS lookahead_template_instances (
        id               TEXT    PRIMARY KEY,
        template_id      TEXT    NOT NULL,
        template_version INTEGER NOT NULL DEFAULT 1,
        start_date       TEXT    NOT NULL DEFAULT '',
        project_tag      TEXT    NOT NULL DEFAULT '',
        owner            TEXT    NOT NULL DEFAULT '',
        status           TEXT    NOT NULL DEFAULT 'active',
        created_at       TEXT    NOT NULL DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_la_tpl_instances_tpl
        ON lookahead_template_instances(template_id);
    CREATE INDEX IF NOT EXISTS idx_la_tpl_instances_project
        ON lookahead_template_instances(project_tag);
    CREATE INDEX IF NOT EXISTS idx_la_cards_tpl_instance
        ON lookahead_cards(template_instance_id);
    """)


# ── Row helpers ────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row) if row else None


def _rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ── Item (analysis) operations ────────────────────────────────────────────────

def get_item(item_id: str) -> Optional[dict]:
    """Fetch a single item by item_id."""
    c = conn()
    row = c.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
    return _row_to_dict(row)


def get_all_items() -> list[dict]:
    """Return all item records."""
    return _rows_to_list(conn().execute("SELECT * FROM items").fetchall())


def get_items_by_project(project_tag: str) -> list[dict]:
    """Return all items tagged to a project (handles both single and multi-tag storage)."""
    # Exact match covers single-tag rows; JSON array rows need LIKE + parse check
    rows = _rows_to_list(
        conn().execute(
            "SELECT * FROM items WHERE project_tag = ? OR project_tag LIKE ?",
            (project_tag, f'%"{project_tag}"%'),
        ).fetchall()
    )
    return [r for r in rows if project_tag in parse_project_tags(r.get("project_tag"))]


def get_items_by_conversation(conversation_id: str) -> list[dict]:
    """Return all items in a conversation thread, ordered by timestamp."""
    return _rows_to_list(
        conn().execute(
            "SELECT * FROM items WHERE conversation_id = ? ORDER BY timestamp",
            (conversation_id,),
        ).fetchall()
    )


def get_items_by_situation(situation_id: str) -> list[dict]:
    """Return all items linked to a situation."""
    return _rows_to_list(
        conn().execute(
            "SELECT * FROM items WHERE situation_id = ?", (situation_id,)
        ).fetchall()
    )


def upsert_item(data: dict) -> None:
    """
    Insert or replace an item record.

    All JSON columns (action_items, goals, key_dates, information_items,
    references) are serialised if they arrive as Python objects.
    """
    c = conn()
    for col in ("action_items", "goals", "key_dates", "information_items", "references"):
        if col in data and not isinstance(data[col], str):
            data[col] = json.dumps(data[col])

    cols   = list(data.keys())
    values = [data[k] for k in cols]
    placeholders = ", ".join("?" * len(cols))
    col_names    = ", ".join(f'"{k}"' for k in cols)
    updates = ", ".join(f'"{c}"=excluded."{c}"' for c in cols if c != "item_id")

    c.execute(
        f"INSERT INTO items ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT(item_id) DO UPDATE SET {updates}",
        values,
    )


def update_item(item_id: str, updates: dict) -> None:
    """Apply a partial update to an item row."""
    if not updates:
        return
    c = conn()
    set_clause = ", ".join(f'"{k}" = ?' for k in updates)
    values     = list(updates.values()) + [item_id]
    c.execute(f"UPDATE items SET {set_clause} WHERE item_id = ?", values)


def update_items_by_project(project_tag: str, updates: dict) -> None:
    """Apply a partial update to all items tagged to a project (handles multi-tag).

    When updates contains project_tag=None, removes the given tag from multi-tag
    items rather than NULLing the whole column.
    """
    if not updates:
        return
    items = get_items_by_project(project_tag)
    c = conn()
    clearing_tag = "project_tag" in updates and updates["project_tag"] is None
    for item in items:
        item_updates = dict(updates)
        if clearing_tag:
            # Remove just this tag; keep others
            tags = [t for t in parse_project_tags(item.get("project_tag")) if t != project_tag]
            item_updates["project_tag"] = serialize_project_tags(tags)
        set_clause = ", ".join(f'"{k}" = ?' for k in item_updates)
        values = list(item_updates.values()) + [item["item_id"]]
        c.execute(f"UPDATE items SET {set_clause} WHERE item_id = ?", values)


def count_items() -> int:
    """Return total item count."""
    return conn().execute("SELECT COUNT(*) FROM items").fetchone()[0]


def get_items_with_pending_batch() -> list[dict]:
    """Return all items that have a batch_job_id set (awaiting batch result)."""
    return _rows_to_list(
        conn().execute(
            "SELECT * FROM items WHERE batch_job_id IS NOT NULL"
        ).fetchall()
    )


def set_batch_job_id(item_id: str, batch_job_id: Optional[str]) -> None:
    """Set or clear the batch_job_id on an item row."""
    conn().execute(
        "UPDATE items SET batch_job_id = ? WHERE item_id = ?",
        (batch_job_id, item_id),
    )


# ── Todo operations ────────────────────────────────────────────────────────────

def get_todos(
    done: bool = False,
    source: Optional[str] = None,
    priority: Optional[str] = None,
    project_tag: Optional[str] = None,
) -> list[dict]:
    """Return todo rows with optional filters, sorted by priority then created_at."""
    c    = conn()
    sql  = "SELECT * FROM todos WHERE 1=1"
    args = []
    if not done:
        sql  += " AND done = 0"
    if source:
        sql  += " AND source = ?"; args.append(source)
    if priority:
        sql  += " AND priority = ?"; args.append(priority)
    if project_tag:
        sql  += " AND (project_tag = ? OR project_tag LIKE ?)"; args.extend([project_tag, f'%"{project_tag}"%'])
    sql += " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at"
    return _rows_to_list(c.execute(sql, args).fetchall())


def get_todos_for_item(item_id: str) -> list[dict]:
    """Return all todos linked to a specific item."""
    return _rows_to_list(
        conn().execute("SELECT * FROM todos WHERE item_id = ?", (item_id,)).fetchall()
    )


def todo_exists(item_id: str, description: str) -> bool:
    """Check if a todo with this item_id+description already exists."""
    row = conn().execute(
        "SELECT 1 FROM todos WHERE item_id = ? AND description = ?",
        (item_id, description),
    ).fetchone()
    return row is not None


_WS_RE = re.compile(r"\s+")


def _norm_desc(s: str) -> str:
    # Whitespace/punctuation/case normalization for cross-item dedup (parsival#77).
    # Paraphrase drift is out of scope — embedding-based near-dup is the follow-up.
    return _WS_RE.sub(" ", (s or "").strip()).rstrip(".!?").lower()


def todo_exists_in_conversation(conversation_id: str, description: str) -> bool:
    """True if any item in this conversation already has a matching todo.

    A single Outlook reply chain shares one conversation_id across many
    item_ids, so per-item todo_exists misses duplicates the LLM re-emits on
    each reply. Widens the scope to the thread and normalizes descriptions.
    """
    if not conversation_id:
        return False
    rows = conn().execute(
        "SELECT t.description FROM todos t JOIN items i ON t.item_id = i.item_id "
        "WHERE i.conversation_id = ?",
        (conversation_id,),
    ).fetchall()
    target = _norm_desc(description)
    return any(_norm_desc(r[0]) == target for r in rows)


def get_open_todos_for_conversation(
    conversation_id: str | None,
    before_timestamp: str | None = None,
    limit: int = 15,
) -> list[dict]:
    """Return open todos saved for earlier items in this conversation.

    Feeds the LLM a "do not re-emit these" hint when analyzing the next
    message in a thread (parsival#79). Paraphrase-level dedup the exact /
    normalized check in todo_exists_in_conversation cannot catch.

    Scoped by ``items.conversation_id``; filtered to ``todos.done = 0``.
    When ``before_timestamp`` is set, only todos from items with a strictly
    earlier ``items.timestamp`` are returned — required on reanalyze so a
    message does not self-suppress its own todos. Most recent ``limit``
    items win (prompt-bloat guard on very long threads).
    """
    if not conversation_id:
        return []
    params: list = [conversation_id]
    where_extra = ""
    if before_timestamp:
        where_extra = " AND i.timestamp < ?"
        params.append(before_timestamp)
    params.append(int(limit))
    rows = conn().execute(
        "SELECT t.description, t.owner, t.deadline "
        "FROM todos t JOIN items i ON t.item_id = i.item_id "
        "WHERE i.conversation_id = ? AND t.done = 0" + where_extra + " "
        "ORDER BY i.timestamp DESC LIMIT ?",
        params,
    ).fetchall()
    return [
        {"description": r[0], "owner": r[1], "deadline": r[2]}
        for r in rows
    ]


def insert_todo(data: dict) -> int:
    """Insert a todo row and return its auto-generated id."""
    c    = conn()
    cols = list(data.keys())
    placeholders = ", ".join("?" * len(cols))
    col_names    = ", ".join(cols)
    cur = c.execute(
        f"INSERT INTO todos ({col_names}) VALUES ({placeholders})",
        [data[k] for k in cols],
    )
    return cur.lastrowid


def update_todo(todo_id: int, updates: dict) -> None:
    """Apply a partial update to a todo row by id."""
    if not updates:
        return
    c = conn()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values     = list(updates.values()) + [todo_id]
    c.execute(f"UPDATE todos SET {set_clause} WHERE id = ?", values)


def update_todos_for_item(item_id: str, updates: dict) -> None:
    """Apply a partial update to all todos linked to an item."""
    if not updates:
        return
    c = conn()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values     = list(updates.values()) + [item_id]
    c.execute(f"UPDATE todos SET {set_clause} WHERE item_id = ?", values)


def delete_todos_for_item(item_id: str) -> None:
    """Remove all todos linked to an item."""
    conn().execute("DELETE FROM todos WHERE item_id = ?", (item_id,))


def delete_todo_by_id(todo_id: int) -> None:
    """Remove a single todo by its integer id."""
    conn().execute("DELETE FROM todos WHERE id = ?", (todo_id,))


def get_all_todos() -> list[dict]:
    """Return all todo rows (including done), ordered by priority then created_at."""
    return _rows_to_list(
        conn().execute(
            "SELECT * FROM todos ORDER BY "
            "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at"
        ).fetchall()
    )


def get_todo_by_id(todo_id: int) -> Optional[dict]:
    """Fetch a single todo by its integer id."""
    row = conn().execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    return _row_to_dict(row)


def count_assigned_open() -> int:
    """Return the number of open todos in the 'assigned' state with a non-empty assigned_to.

    Uses the idx_todos_status(done, status) index to avoid pulling full rows —
    the Assigned vtab badge only needs the count, not the payload.
    """
    row = conn().execute(
        "SELECT COUNT(*) FROM todos "
        "WHERE done = 0 AND status = 'assigned' "
        "AND assigned_to IS NOT NULL AND assigned_to != ''"
    ).fetchone()
    return int(row[0]) if row else 0


# ── Intel operations ───────────────────────────────────────────────────────────

def intel_exists(item_id: str, fact: str) -> bool:
    """Check if an intel row with this item_id+fact already exists."""
    row = conn().execute(
        "SELECT 1 FROM intel WHERE item_id = ? AND fact = ?",
        (item_id, fact),
    ).fetchone()
    return row is not None


def insert_intel(data: dict) -> None:
    """Insert an intel row."""
    c    = conn()
    cols = list(data.keys())
    placeholders = ", ".join("?" * len(cols))
    col_names    = ", ".join(cols)
    c.execute(
        f"INSERT INTO intel ({col_names}) VALUES ({placeholders})",
        [data[k] for k in cols],
    )


def get_intel_for_item(item_id: str) -> list[dict]:
    """Return all intel rows for an item."""
    return _rows_to_list(
        conn().execute(
            "SELECT * FROM intel WHERE item_id = ? AND dismissed = 0", (item_id,)
        ).fetchall()
    )


def get_intel_for_items(item_ids: list) -> list[dict]:
    """Return all non-dismissed intel rows for a set of item_ids."""
    if not item_ids:
        return []
    placeholders = ", ".join("?" * len(item_ids))
    return _rows_to_list(
        conn().execute(
            f"SELECT * FROM intel WHERE item_id IN ({placeholders}) AND dismissed = 0",
            item_ids,
        ).fetchall()
    )


def get_all_intel(dismissed: bool = False) -> list[dict]:
    """Return all intel rows, optionally including dismissed ones."""
    sql = "SELECT * FROM intel" if dismissed else "SELECT * FROM intel WHERE dismissed = 0"
    return _rows_to_list(conn().execute(sql).fetchall())


def delete_intel_for_item(item_id: str) -> None:
    """Remove all intel rows for an item."""
    conn().execute("DELETE FROM intel WHERE item_id = ?", (item_id,))


def delete_intel_by_id(intel_id: int) -> None:
    """Remove a single intel row by its integer id."""
    conn().execute("DELETE FROM intel WHERE id = ?", (intel_id,))


def update_intel_by_id(intel_id: int, updates: dict) -> None:
    """Apply a partial update to an intel row by id."""
    if not updates:
        return
    c = conn()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values     = list(updates.values()) + [intel_id]
    c.execute(f"UPDATE intel SET {set_clause} WHERE id = ?", values)


def update_intel_project(item_id: str, project_tag) -> None:
    """Sync project_tag on all intel rows for an item.

    Accepts a single tag string, a list of tags, or a serialized JSON array.
    Intel rows store a single tag (the first/primary), since each intel fact
    typically belongs to one project context.
    """
    # Intel rows keep a single tag — use the first from a multi-tag value
    if isinstance(project_tag, list):
        primary = project_tag[0] if project_tag else None
    else:
        tags = parse_project_tags(project_tag)
        primary = tags[0] if tags else None
    conn().execute(
        "UPDATE intel SET project_tag = ? WHERE item_id = ?", (primary, item_id)
    )


# ── Situation operations ───────────────────────────────────────────────────────

def _parse_situation(d: dict) -> dict:
    """Parse JSON list columns on a situation dict in-place and return it."""
    if d is None:
        return None
    for col in ("item_ids", "sources", "open_actions", "references"):
        v = d.get(col)
        if isinstance(v, str):
            try:
                d[col] = json.loads(v)
            except Exception:
                d[col] = []
    return d


def get_situation(situation_id: str) -> Optional[dict]:
    """Fetch a single situation by situation_id, with JSON list columns parsed."""
    row = conn().execute(
        "SELECT * FROM situations WHERE situation_id = ?", (situation_id,)
    ).fetchone()
    return _parse_situation(_row_to_dict(row))


def get_all_situations(include_dismissed: bool = False) -> list[dict]:
    """Return all situations with JSON list columns parsed."""
    sql = "SELECT * FROM situations" if include_dismissed \
          else "SELECT * FROM situations WHERE dismissed = 0"
    return [_parse_situation(d) for d in _rows_to_list(conn().execute(sql).fetchall())]


def insert_situation(data: dict) -> None:
    """Insert a new situation record."""
    c = conn()
    for col in ("item_ids", "sources", "open_actions", "references"):
        if col in data and not isinstance(data[col], str):
            data[col] = json.dumps(data[col])
    cols         = list(data.keys())
    placeholders = ", ".join("?" * len(cols))
    col_names    = ", ".join(f'"{k}"' for k in cols)
    c.execute(
        f"INSERT INTO situations ({col_names}) VALUES ({placeholders})",
        [data[k] for k in cols],
    )


def update_situation(situation_id: str, updates: dict) -> None:
    """Apply a partial update to a situation record."""
    if not updates:
        return
    c = conn()
    for col in ("item_ids", "sources", "open_actions", "references"):
        if col in updates and not isinstance(updates[col], str):
            updates[col] = json.dumps(updates[col])
    set_clause = ", ".join(f'"{k}" = ?' for k in updates)
    values     = list(updates.values()) + [situation_id]
    c.execute(f"UPDATE situations SET {set_clause} WHERE situation_id = ?", values)


def delete_situation(situation_id: str) -> None:
    """Delete a situation record."""
    conn().execute("DELETE FROM situations WHERE situation_id = ?", (situation_id,))


def get_situations_containing_item(item_id: str) -> list[dict]:
    """Return all situations (including dismissed) where item_ids contains item_id."""
    # item_ids stored as JSON array; filter in Python after fetching all
    return [s for s in get_all_situations(include_dismissed=True)
            if item_id in s.get("item_ids", [])]


def get_active_situations(lifecycle_statuses: list[str] | None = None) -> list[dict]:
    """
    Return situations filtered by lifecycle_status.

    If ``lifecycle_statuses`` is None, defaults to active statuses:
    ``new``, ``investigating``, ``waiting``.
    """
    if lifecycle_statuses is None:
        lifecycle_statuses = ["new", "investigating", "waiting"]
    placeholders = ", ".join("?" * len(lifecycle_statuses))
    rows = conn().execute(
        f"SELECT * FROM situations WHERE lifecycle_status IN ({placeholders})",
        lifecycle_statuses,
    ).fetchall()
    return [_parse_situation(_row_to_dict(r)) for r in rows]


def insert_situation_event(situation_id: str, from_status: str | None,
                           to_status: str, note: str | None = None) -> None:
    """Log a lifecycle status transition event."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    conn().execute(
        "INSERT INTO situation_events (situation_id, from_status, to_status, timestamp, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (situation_id, from_status, to_status, ts, note),
    )


def get_situation_events(situation_id: str) -> list[dict]:
    """Return all lifecycle events for a situation, oldest first."""
    rows = conn().execute(
        "SELECT * FROM situation_events WHERE situation_id=? ORDER BY id ASC",
        (situation_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── User actions (attention model) ────────────────────────────────────────────

def record_user_action(item_id: str, action_type: str) -> None:
    """Log a user interaction with an item for attention model training."""
    ts = _now_iso()
    conn().execute(
        "INSERT INTO user_actions (item_id, action_type, timestamp) VALUES (?,?,?)",
        (item_id, action_type, ts),
    )


def get_user_actions(since_iso: str | None = None) -> list[dict]:
    """Return user actions, optionally filtered to those after *since_iso*."""
    if since_iso:
        rows = conn().execute(
            "SELECT * FROM user_actions WHERE timestamp >= ? ORDER BY id ASC",
            (since_iso,),
        ).fetchall()
    else:
        rows = conn().execute("SELECT * FROM user_actions ORDER BY id ASC").fetchall()
    return [_row_to_dict(r) for r in rows]


def count_user_actions() -> int:
    """Return total number of recorded user actions."""
    return conn().execute("SELECT COUNT(*) FROM user_actions").fetchone()[0]


# ── Model state (attention centroids) ─────────────────────────────────────────

def get_model_state(key: str) -> Optional[dict]:
    """Return a deserialized model state value, or None."""
    row = conn().execute("SELECT value FROM model_state WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def set_model_state(key: str, value: dict) -> None:
    """Upsert a model state value (serialized as JSON)."""
    conn().execute(
        "INSERT INTO model_state (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


# ── Settings operations ────────────────────────────────────────────────────────

def get_settings() -> dict:
    """Return the settings blob as a dict."""
    row = conn().execute("SELECT data FROM settings WHERE id = 1").fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["data"])
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    """Persist the settings blob (upsert on id=1)."""
    c = conn()
    payload = json.dumps(data)
    c.execute(
        "INSERT INTO settings (id, data) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
        (payload,),
    )


# ── Briefing operations ────────────────────────────────────────────────────────

def save_briefing(content: dict) -> None:
    """Persist the latest briefing, replacing any previous one."""
    c = conn()
    c.execute("DELETE FROM briefings")
    c.execute(
        "INSERT INTO briefings (generated_at, content) VALUES (?, ?)",
        (_now_iso(), json.dumps(content)),
    )


def get_briefing() -> dict | None:
    """Return the latest briefing, or None if none exists."""
    row = conn().execute(
        "SELECT generated_at, content FROM briefings ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        content = json.loads(row["content"])
    except Exception:
        content = {}
    return {"generated_at": row["generated_at"], **content}


# ── Scan log operations ────────────────────────────────────────────────────────

def insert_scan_log(data: dict) -> None:
    """Insert a scan log entry."""
    c    = conn()
    cols = list(data.keys())
    placeholders = ", ".join("?" * len(cols))
    col_names    = ", ".join(cols)
    c.execute(
        f"INSERT INTO scan_logs ({col_names}) VALUES ({placeholders})",
        [data[k] for k in cols],
    )


def get_scan_logs(limit: int = 20) -> list[dict]:
    """Return the most recent scan log entries."""
    return _rows_to_list(
        conn().execute(
            "SELECT * FROM scan_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    )


def get_all_scan_logs() -> list[dict]:
    """Return all scan log entries, newest first."""
    return _rows_to_list(
        conn().execute("SELECT * FROM scan_logs ORDER BY id DESC").fetchall()
    )


# ── Embedding operations ───────────────────────────────────────────────────────

def get_embedding(project: str) -> Optional[dict]:
    """Return the embedding record for a project."""
    row = conn().execute(
        "SELECT * FROM embeddings WHERE project = ?", (project,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    for col in ("items", "centroids", "centroid_counts"):
        if isinstance(d.get(col), str):
            try:
                d[col] = json.loads(d[col])
            except Exception:
                d[col] = {} if col != "items" else []
    return d


def upsert_embedding(project: str, items: list, centroids: dict, centroid_counts: dict) -> None:
    """Upsert the embedding record for a project."""
    c = conn()
    c.execute(
        "INSERT INTO embeddings (project, items, centroids, centroid_counts) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(project) DO UPDATE SET "
        "items=excluded.items, centroids=excluded.centroids, centroid_counts=excluded.centroid_counts",
        (project, json.dumps(items), json.dumps(centroids), json.dumps(centroid_counts)),
    )


def get_all_embeddings() -> list[dict]:
    """Return all embedding records with JSON columns parsed."""
    rows = _rows_to_list(conn().execute("SELECT * FROM embeddings").fetchall())
    for r in rows:
        for col in ("items", "centroids", "centroid_counts"):
            if isinstance(r.get(col), str):
                try:
                    r[col] = json.loads(r[col])
                except Exception:
                    r[col] = {} if col != "items" else []
    return rows


def delete_embedding_project(project: str) -> None:
    """Remove the embedding record for a project."""
    conn().execute("DELETE FROM embeddings WHERE project = ?", (project,))


# ── Reset ──────────────────────────────────────────────────────────────────────

def reset_data_tables() -> None:
    """Truncate all data tables while preserving settings."""
    c = conn()
    for table in ("items", "todos", "intel", "situations", "scan_logs",
                  "embeddings", "nodes", "edges"):
        c.execute(f"DELETE FROM {table}")


# ── Graph operations ───────────────────────────────────────────────────────────

def upsert_node(node_id: str, node_type: str, label: str, properties: dict = None) -> None:
    """Insert or update a graph node."""
    c = conn()
    c.execute(
        "INSERT INTO nodes (node_id, node_type, label, properties) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(node_id) DO UPDATE SET "
        "node_type=excluded.node_type, label=excluded.label, properties=excluded.properties",
        (node_id, node_type, label, json.dumps(properties or {})),
    )


def upsert_edge(
    src_id: str,
    dst_id: str,
    edge_type: str,
    weight: float = 1.0,
    properties: dict = None,
) -> None:
    """
    Insert or update a graph edge.  Weight is updated to the new value if the
    edge already exists (e.g. accumulated co-occurrence count).
    """
    c = conn()
    c.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, weight, created_at, properties) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(src_id, dst_id, edge_type) DO UPDATE SET "
        "weight=excluded.weight, created_at=excluded.created_at",
        (src_id, dst_id, edge_type, weight, _now_iso(), json.dumps(properties or {})),
    )


def get_edges_from(node_id: str, edge_type: Optional[str] = None) -> list[dict]:
    """Return all edges where src_id matches."""
    c    = conn()
    if edge_type:
        rows = c.execute(
            "SELECT * FROM edges WHERE src_id = ? AND edge_type = ?",
            (node_id, edge_type),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM edges WHERE src_id = ?", (node_id,)
        ).fetchall()
    return _rows_to_list(rows)


def get_edges_to(node_id: str, edge_type: Optional[str] = None) -> list[dict]:
    """Return all edges where dst_id matches."""
    c    = conn()
    if edge_type:
        rows = c.execute(
            "SELECT * FROM edges WHERE dst_id = ? AND edge_type = ?",
            (node_id, edge_type),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM edges WHERE dst_id = ?", (node_id,)
        ).fetchall()
    return _rows_to_list(rows)


def get_node(node_id: str) -> Optional[dict]:
    """Fetch a single node by id."""
    row = conn().execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
    return _row_to_dict(row)


def get_nodes_by_type(node_type: str) -> list[dict]:
    """Return all nodes of a given type."""
    return _rows_to_list(
        conn().execute("SELECT * FROM nodes WHERE node_type = ?", (node_type,)).fetchall()
    )


# ── Contacts operations ───────────────────────────────────────────────────────
#
# Identity is a stable serial integer (contact_id).  Emails live in the
# contact_emails join table — every helper that returns a contact dict
# attaches an "emails" key with the full list, primary first.

def _attach_emails(contact: Optional[dict]) -> Optional[dict]:
    """Attach the joined emails list to a contact dict (in place).

    Also decodes the JSON-typed columns (``manually_edited_fields``,
    ``signature_confidence``) into native Python types so callers (and the
    API layer) get a list/dict instead of a raw string.
    """
    if not contact:
        return contact
    rows = conn().execute(
        "SELECT email, is_primary FROM contact_emails "
        "WHERE contact_id = ? ORDER BY is_primary DESC, added_at",
        (contact["contact_id"],),
    ).fetchall()
    contact["emails"] = [r["email"] for r in rows]
    contact["primary_email"] = next(
        (r["email"] for r in rows if r["is_primary"]),
        contact["emails"][0] if contact["emails"] else None,
    )
    # Decode JSON columns to native types.  Tolerate legacy/empty values.
    raw_edited = contact.get("manually_edited_fields") or "[]"
    if isinstance(raw_edited, str):
        try:
            contact["manually_edited_fields"] = json.loads(raw_edited)
        except (json.JSONDecodeError, TypeError):
            contact["manually_edited_fields"] = []
    raw_conf = contact.get("signature_confidence") or "{}"
    if isinstance(raw_conf, str):
        try:
            contact["signature_confidence"] = json.loads(raw_conf)
        except (json.JSONDecodeError, TypeError):
            contact["signature_confidence"] = {}
    return contact


def get_contact(contact_id: int) -> Optional[dict]:
    """Fetch a single contact by id, with emails attached."""
    row = conn().execute(
        "SELECT * FROM contacts WHERE contact_id = ?", (contact_id,)
    ).fetchone()
    return _attach_emails(_row_to_dict(row))


def get_contact_by_email(email: str) -> Optional[dict]:
    """Look up a contact by any of its email addresses (case-insensitive)."""
    if not email:
        return None
    row = conn().execute(
        "SELECT c.* FROM contacts c "
        "JOIN contact_emails e ON e.contact_id = c.contact_id "
        "WHERE LOWER(e.email) = LOWER(?)",
        (email,),
    ).fetchone()
    return _attach_emails(_row_to_dict(row))


def find_contacts_by_name(name: str) -> list[dict]:
    """Substring (case-insensitive) match on contact name. Used by owner resolution."""
    if not name:
        return []
    pattern = f"%{name.strip()}%"
    rows = conn().execute(
        "SELECT * FROM contacts WHERE LOWER(name) LIKE LOWER(?) "
        "ORDER BY source_count DESC, last_seen DESC",
        (pattern,),
    ).fetchall()
    return [_attach_emails(dict(r)) for r in rows]


def list_contacts(query: Optional[str] = None, limit: int = 500) -> list[dict]:
    """Return contacts ordered by most-recently-seen, optionally filtered.

    `query` matches against name, employer, title, or any associated email
    (case-insensitive substring).
    """
    c = conn()
    if query:
        pattern = f"%{query.strip()}%"
        rows = c.execute(
            "SELECT DISTINCT c.* FROM contacts c "
            "LEFT JOIN contact_emails e ON e.contact_id = c.contact_id "
            "WHERE LOWER(c.name)     LIKE LOWER(?) "
            "   OR LOWER(c.employer) LIKE LOWER(?) "
            "   OR LOWER(c.title)    LIKE LOWER(?) "
            "   OR LOWER(e.email)    LIKE LOWER(?) "
            "ORDER BY c.last_seen DESC, c.contact_id DESC LIMIT ?",
            (pattern, pattern, pattern, pattern, limit),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM contacts ORDER BY last_seen DESC, contact_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_attach_emails(dict(r)) for r in rows]


def count_contacts() -> int:
    """Return total contact count."""
    return conn().execute("SELECT COUNT(*) FROM contacts").fetchone()[0]


def insert_contact(data: dict) -> int:
    """Insert a new contact row and return its assigned contact_id.

    `data` may contain any contact column plus an optional "emails" list.
    Emails are inserted into contact_emails; the first becomes primary.

    Manually-created contacts (``is_manual=True``) default every field's
    provenance to ``manual`` and seed ``manually_edited_fields`` with the
    fields the caller actually populated, so the signature parser will not
    later clobber what the user typed in by hand.
    """
    c = conn()
    now = _now_iso()
    is_manual = bool(data.get("is_manual"))

    # For manual contacts, every populated field is implicitly "user typed
    # this" and locked from the signature parser.
    auto_edited: list[str] = []
    if is_manual:
        for field in ("name", "phone", "employer", "title", "employer_address"):
            if data.get(field):
                auto_edited.append(field)

    default_source = "manual" if is_manual else "header"
    payload = {
        "name":             data.get("name", "") or "",
        "phone":            data.get("phone", "") or "",
        "employer":         data.get("employer", "") or "",
        "title":            data.get("title", "") or "",
        "employer_address": data.get("employer_address", "") or "",
        "notes":            data.get("notes", "") or "",
        "first_seen":       data.get("first_seen") or now,
        "last_seen":        data.get("last_seen")  or now,
        "source_count":     data.get("source_count", 0),
        "last_item_id":     data.get("last_item_id"),
        "is_manual":        1 if is_manual else 0,
        "name_source":      data.get("name_source")     or default_source,
        "phone_source":     data.get("phone_source")    or default_source,
        "employer_source":  data.get("employer_source") or default_source,
        "title_source":     data.get("title_source")    or default_source,
        "address_source":   data.get("address_source")  or default_source,
        "manually_edited_fields": json.dumps(
            data.get("manually_edited_fields") or auto_edited
        ),
        "signature_confidence":   json.dumps(
            data.get("signature_confidence") or {}
        ),
        "created_at":       now,
        "updated_at":       now,
    }
    cols   = list(payload.keys())
    values = [payload[k] for k in cols]
    placeholders = ", ".join("?" * len(cols))
    col_names    = ", ".join(f'"{k}"' for k in cols)
    cur = c.execute(
        f"INSERT INTO contacts ({col_names}) VALUES ({placeholders})",
        values,
    )
    contact_id = cur.lastrowid

    emails = data.get("emails") or []
    for idx, email in enumerate(emails):
        if not email:
            continue
        try:
            c.execute(
                "INSERT INTO contact_emails (contact_id, email, is_primary, added_at) "
                "VALUES (?, ?, ?, ?)",
                (contact_id, email.lower(), 1 if idx == 0 else 0, now),
            )
        except sqlite3.IntegrityError:
            # email already attached to a different contact — skip
            pass
    return contact_id


def update_contact(contact_id: int, updates: dict) -> None:
    """Apply a partial update to a contact row.  Ignores unknown columns.

    JSON-typed columns (``manually_edited_fields``, ``signature_confidence``)
    are serialised here when callers pass list/dict values.  Source columns
    are passed through verbatim — callers like the signature parser stamp
    them explicitly, and the manual PATCH path in app.py adds its own
    'manual' stamping wrapper around this helper.
    """
    if not updates:
        return
    allowed = {
        "name", "phone", "employer", "title", "employer_address", "notes",
        "first_seen", "last_seen", "source_count", "last_item_id", "is_manual",
        "name_source", "phone_source", "employer_source", "title_source",
        "address_source",
        "manually_edited_fields", "signature_confidence",
    }
    clean = {k: v for k, v in updates.items() if k in allowed}
    if not clean:
        return
    # Serialise structured columns if the caller passed a list/dict.
    for json_col in ("manually_edited_fields", "signature_confidence"):
        if json_col in clean and not isinstance(clean[json_col], str):
            clean[json_col] = json.dumps(clean[json_col] or ([] if json_col.endswith("fields") else {}))
    clean["updated_at"] = _now_iso()
    set_clause = ", ".join(f'"{k}" = ?' for k in clean)
    values     = list(clean.values()) + [contact_id]
    conn().execute(
        f"UPDATE contacts SET {set_clause} WHERE contact_id = ?", values
    )


def delete_contact(contact_id: int) -> None:
    """Delete a contact and all its emails (cascade via FK)."""
    conn().execute("DELETE FROM contacts WHERE contact_id = ?", (contact_id,))


def add_contact_email(contact_id: int, email: str, is_primary: bool = False) -> bool:
    """Attach an email to a contact.  Returns False if the email is already
    attached to another contact (caller can decide whether to merge)."""
    if not email:
        return False
    c = conn()
    email = email.lower()
    existing = c.execute(
        "SELECT contact_id FROM contact_emails WHERE LOWER(email) = ?",
        (email,),
    ).fetchone()
    if existing:
        return existing["contact_id"] == contact_id
    if is_primary:
        c.execute(
            "UPDATE contact_emails SET is_primary = 0 WHERE contact_id = ?",
            (contact_id,),
        )
    c.execute(
        "INSERT INTO contact_emails (contact_id, email, is_primary, added_at) "
        "VALUES (?, ?, ?, ?)",
        (contact_id, email, 1 if is_primary else 0, _now_iso()),
    )
    return True


def remove_contact_email(contact_id: int, email: str) -> None:
    """Detach an email from a contact."""
    conn().execute(
        "DELETE FROM contact_emails WHERE contact_id = ? AND LOWER(email) = LOWER(?)",
        (contact_id, email),
    )


def upsert_contact_from_header(
    display_name: str,
    email: str,
    item_id: Optional[str] = None,
    item_timestamp: Optional[str] = None,
) -> int:
    """Idempotently record a contact seen in an email header.

    Lookup is by email (the only stable thing we have in a header).  When the
    email is new, a contact is created using the display name.  When the email
    already exists, source_count and last_seen are bumped, and the name is
    filled in if it was previously empty.

    Returns the contact_id.
    """
    c = conn()
    now = _now_iso()
    seen_at = item_timestamp or now
    email_lc = (email or "").strip().lower()
    if not email_lc:
        # No email — fall back to a name-only contact (rare; e.g. just author).
        cur = c.execute(
            "INSERT INTO contacts (name, first_seen, last_seen, source_count, "
            "last_item_id, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
            (display_name or "", seen_at, seen_at, item_id, now, now),
        )
        return cur.lastrowid

    existing = c.execute(
        "SELECT c.* FROM contacts c "
        "JOIN contact_emails e ON e.contact_id = c.contact_id "
        "WHERE LOWER(e.email) = ?",
        (email_lc,),
    ).fetchone()

    if existing:
        contact_id = existing["contact_id"]
        # Bump counters and last_seen; fill in name if missing.
        new_name = existing["name"] or (display_name or "").strip()
        first_seen = min(existing["first_seen"] or seen_at, seen_at)
        last_seen  = max(existing["last_seen"]  or seen_at, seen_at)
        c.execute(
            "UPDATE contacts SET name = ?, first_seen = ?, last_seen = ?, "
            "source_count = source_count + 1, last_item_id = ?, updated_at = ? "
            "WHERE contact_id = ?",
            (new_name, first_seen, last_seen, item_id, now, contact_id),
        )
        return contact_id

    # New contact.
    cur = c.execute(
        "INSERT INTO contacts (name, first_seen, last_seen, source_count, "
        "last_item_id, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
        ((display_name or "").strip(), seen_at, seen_at, item_id, now, now),
    )
    contact_id = cur.lastrowid
    c.execute(
        "INSERT INTO contact_emails (contact_id, email, is_primary, added_at) "
        "VALUES (?, ?, 1, ?)",
        (contact_id, email_lc, now),
    )
    return contact_id


# ── Look-ahead board (parsival#48) ────────────────────────────────────────────
#
# Cards, dependencies, links, resources and per-project shift schedules power
# the two-week look-ahead view.  Helpers return plain dicts (with relation
# lists inlined) so the API layer can hand them straight to the frontend.

_CARD_STATUSES   = ("planned", "in_progress", "done", "blocked")
_RESOURCE_TYPES  = ("person", "equipment", "space", "part", "supply")
_RESOURCE_STATUSES = ("needed", "secured", "consumed")
_LINK_TYPES      = ("todo", "situation", "key_date", "item")


def _card_with_relations(row: dict) -> dict:
    """Expand a lookahead_cards row with deps/links/resources inlined."""
    if not row:
        return None
    c = conn()
    cid = row["id"]
    row["depends_on"] = [
        r["depends_on_id"] for r in c.execute(
            "SELECT depends_on_id FROM lookahead_card_deps WHERE card_id = ?", (cid,)
        ).fetchall()
    ]
    row["links"] = [
        {"type": r["link_type"], "id": r["target_id"]}
        for r in c.execute(
            "SELECT link_type, target_id FROM lookahead_card_links WHERE card_id = ?", (cid,)
        ).fetchall()
    ]
    row["resources"] = [
        dict(r) for r in c.execute(
            "SELECT cr.resource_id, cr.quantity, cr.status, r.name, r.type "
            "FROM lookahead_card_resources cr "
            "JOIN lookahead_resources r ON r.id = cr.resource_id "
            "WHERE cr.card_id = ? ORDER BY r.name",
            (cid,),
        ).fetchall()
    ]
    return row


def get_lookahead_card(card_id: str) -> Optional[dict]:
    row = conn().execute("SELECT * FROM lookahead_cards WHERE id = ?", (card_id,)).fetchone()
    return _card_with_relations(_row_to_dict(row)) if row else None


def list_lookahead_cards(project: Optional[str] = None,
                         start_date: Optional[str] = None,
                         end_date: Optional[str] = None) -> list[dict]:
    """List cards, optionally filtered by project and an overlapping date window."""
    sql  = "SELECT * FROM lookahead_cards"
    args: list = []
    where: list[str] = []
    if project:
        where.append("project = ?")
        args.append(project)
    if start_date and end_date:
        # Include cards whose span overlaps [start_date, end_date].
        where.append("NOT (end_date < ? OR start_date > ?)")
        args.extend([start_date, end_date])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY start_date, start_shift_num, id"
    rows = _rows_to_list(conn().execute(sql, args).fetchall())
    return [_card_with_relations(r) for r in rows]


def upsert_lookahead_card(data: dict) -> dict:
    """Insert or update a card; ``data`` must include ``id``."""
    cid = data["id"]
    now = _now_iso()
    c = conn()
    existing = c.execute("SELECT id FROM lookahead_cards WHERE id = ?", (cid,)).fetchone()
    if existing:
        updates = {k: v for k, v in data.items() if k != "id"}
        updates["updated_at"] = now
        set_clause = ", ".join(f'"{k}" = ?' for k in updates)
        c.execute(
            f"UPDATE lookahead_cards SET {set_clause} WHERE id = ?",
            list(updates.values()) + [cid],
        )
    else:
        data.setdefault("created_at", now)
        data.setdefault("updated_at", now)
        cols   = list(data.keys())
        placeholders = ", ".join("?" * len(cols))
        col_names    = ", ".join(f'"{k}"' for k in cols)
        c.execute(
            f"INSERT INTO lookahead_cards ({col_names}) VALUES ({placeholders})",
            [data[k] for k in cols],
        )
    return get_lookahead_card(cid)


def delete_lookahead_card(card_id: str) -> None:
    conn().execute("DELETE FROM lookahead_cards WHERE id = ?", (card_id,))


def set_card_dependencies(card_id: str, depends_on_ids: list[str]) -> None:
    c = conn()
    c.execute("DELETE FROM lookahead_card_deps WHERE card_id = ?", (card_id,))
    for dep_id in depends_on_ids or []:
        if dep_id == card_id:
            continue  # self-dependency is meaningless
        c.execute(
            "INSERT OR IGNORE INTO lookahead_card_deps (card_id, depends_on_id) VALUES (?, ?)",
            (card_id, dep_id),
        )


def set_card_links(card_id: str, links: list[dict]) -> None:
    """Replace the user-editable link set on a card. ``links`` is [{type, id}, ...].

    The ``todo`` link type is auto-managed (one todo per card, created on card
    insert, kept in sync by app endpoints) and is preserved across this call
    even if not present in ``links``.
    """
    c = conn()
    c.execute(
        "DELETE FROM lookahead_card_links WHERE card_id = ? AND link_type != 'todo'",
        (card_id,),
    )
    for link in links or []:
        lt = link.get("type")
        tid = link.get("id")
        if lt not in _LINK_TYPES or lt == "todo" or not tid:
            continue
        c.execute(
            "INSERT OR IGNORE INTO lookahead_card_links (card_id, link_type, target_id) "
            "VALUES (?, ?, ?)",
            (card_id, lt, str(tid)),
        )


def get_card_todo_id(card_id: str) -> Optional[int]:
    """Return the integer todo id linked to this card, or None."""
    row = conn().execute(
        "SELECT target_id FROM lookahead_card_links "
        "WHERE card_id = ? AND link_type = 'todo' LIMIT 1",
        (card_id,),
    ).fetchone()
    if not row:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def get_cards_for_todo(todo_id: int) -> list[str]:
    """Return card ids linked to a todo (usually zero or one)."""
    rows = conn().execute(
        "SELECT card_id FROM lookahead_card_links "
        "WHERE link_type = 'todo' AND target_id = ?",
        (str(todo_id),),
    ).fetchall()
    return [r[0] for r in rows]


def set_card_todo_link(card_id: str, todo_id: int) -> None:
    """Replace the card's todo link with a single pointer to ``todo_id``."""
    c = conn()
    c.execute(
        "DELETE FROM lookahead_card_links WHERE card_id = ? AND link_type = 'todo'",
        (card_id,),
    )
    c.execute(
        "INSERT OR IGNORE INTO lookahead_card_links (card_id, link_type, target_id) "
        "VALUES (?, 'todo', ?)",
        (card_id, str(int(todo_id))),
    )


def list_cards_without_todo() -> list[dict]:
    """Return all cards that have no linked todo."""
    rows = conn().execute(
        "SELECT c.* FROM lookahead_cards c "
        "LEFT JOIN lookahead_card_links l "
        "  ON l.card_id = c.id AND l.link_type = 'todo' "
        "WHERE l.card_id IS NULL"
    ).fetchall()
    return _rows_to_list(rows)


def set_card_resources(card_id: str, entries: list[dict]) -> None:
    """Replace the card's BOM entries. ``entries`` is [{resource_id, quantity, status}]."""
    c = conn()
    c.execute("DELETE FROM lookahead_card_resources WHERE card_id = ?", (card_id,))
    for e in entries or []:
        rid = e.get("resource_id")
        if rid is None:
            continue
        qty    = float(e.get("quantity", 1))
        status = e.get("status", "needed")
        if status not in _RESOURCE_STATUSES:
            status = "needed"
        c.execute(
            "INSERT OR REPLACE INTO lookahead_card_resources "
            "(card_id, resource_id, quantity, status) VALUES (?, ?, ?, ?)",
            (card_id, int(rid), qty, status),
        )


def set_card_resource_status(card_id: str, resource_id: int, status: str) -> None:
    if status not in _RESOURCE_STATUSES:
        raise ValueError(f"invalid status: {status}")
    conn().execute(
        "UPDATE lookahead_card_resources SET status = ? WHERE card_id = ? AND resource_id = ?",
        (status, card_id, int(resource_id)),
    )


# ── Resources (global catalog) ────────────────────────────────────────────────

def list_resources(type_filter: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM lookahead_resources"
    args: list = []
    if type_filter:
        sql += " WHERE type = ?"
        args.append(type_filter)
    sql += " ORDER BY type, name"
    return _rows_to_list(conn().execute(sql, args).fetchall())


def get_resource(resource_id: int) -> Optional[dict]:
    row = conn().execute("SELECT * FROM lookahead_resources WHERE id = ?",
                         (int(resource_id),)).fetchone()
    return _row_to_dict(row)


def create_resource(name: str, type_: str, notes: str = "") -> dict:
    if type_ not in _RESOURCE_TYPES:
        raise ValueError(f"invalid resource type: {type_}")
    cur = conn().execute(
        "INSERT INTO lookahead_resources (name, type, notes, created_at) VALUES (?, ?, ?, ?)",
        (name.strip(), type_, notes, _now_iso()),
    )
    return get_resource(cur.lastrowid)


def update_resource(resource_id: int, updates: dict) -> Optional[dict]:
    allowed = {k: v for k, v in updates.items() if k in ("name", "type", "notes")}
    if not allowed:
        return get_resource(resource_id)
    if "type" in allowed and allowed["type"] not in _RESOURCE_TYPES:
        raise ValueError(f"invalid resource type: {allowed['type']}")
    set_clause = ", ".join(f'"{k}" = ?' for k in allowed)
    conn().execute(
        f"UPDATE lookahead_resources SET {set_clause} WHERE id = ?",
        list(allowed.values()) + [int(resource_id)],
    )
    return get_resource(resource_id)


def delete_resource(resource_id: int) -> None:
    conn().execute("DELETE FROM lookahead_resources WHERE id = ?", (int(resource_id),))


# ── Project shift schedules ───────────────────────────────────────────────────

def list_project_shifts(project_tag: Optional[str] = None) -> list[dict]:
    sql  = "SELECT * FROM project_shifts"
    args: list = []
    if project_tag:
        sql += " WHERE project_tag = ?"
        args.append(project_tag)
    sql += " ORDER BY project_tag, shift_num"
    return _rows_to_list(conn().execute(sql, args).fetchall())


def upsert_project_shift(project_tag: str, shift_num: int, data: dict) -> dict:
    """Insert or update a single shift row for a project."""
    label = data.get("label", "")
    start_time = data.get("start_time", "")
    end_time   = data.get("end_time", "")
    days       = data.get("days", "")
    conn().execute(
        "INSERT INTO project_shifts (project_tag, shift_num, label, start_time, end_time, days) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(project_tag, shift_num) DO UPDATE SET "
        "  label = excluded.label, "
        "  start_time = excluded.start_time, "
        "  end_time = excluded.end_time, "
        "  days = excluded.days",
        (project_tag, int(shift_num), label, start_time, end_time, days),
    )
    row = conn().execute(
        "SELECT * FROM project_shifts WHERE project_tag = ? AND shift_num = ?",
        (project_tag, int(shift_num)),
    ).fetchone()
    return _row_to_dict(row)


def delete_project_shift(project_tag: str, shift_num: int) -> None:
    conn().execute(
        "DELETE FROM project_shifts WHERE project_tag = ? AND shift_num = ?",
        (project_tag, int(shift_num)),
    )


# ── Look-ahead templates (parsival#49) ────────────────────────────────────────
#
# A template is a graph of tasks with offsets, deps, and resource needs.
# Instantiating a template copies its tasks into concrete cards at absolute
# dates; the instance PK is stamped on each card's ``template_instance_id``
# so later reschedules can move the whole cohort by the same delta.

_DURATION_UNITS = ("calendar_days", "business_days")
_INSTANCE_STATUSES = ("active", "complete", "cancelled")


def _template_with_tasks(row: dict) -> dict:
    if not row:
        return None
    c = conn()
    tid = row["id"]
    tasks = _rows_to_list(c.execute(
        "SELECT * FROM lookahead_template_tasks "
        "WHERE template_id = ? ORDER BY offset_start_days, offset_start_shift, local_id",
        (tid,),
    ).fetchall())
    # Inline deps and resource requirements per task.
    deps_by_task: dict[str, list[str]] = {}
    for r in c.execute(
        "SELECT task_local_id, depends_on_local_id "
        "FROM lookahead_template_task_deps WHERE template_id = ?",
        (tid,),
    ).fetchall():
        deps_by_task.setdefault(r["task_local_id"], []).append(r["depends_on_local_id"])
    reqs_by_task: dict[str, list[dict]] = {}
    for r in c.execute(
        "SELECT id, task_local_id, resource_type, role, named_resource_id, quantity "
        "FROM lookahead_template_task_resources WHERE template_id = ?",
        (tid,),
    ).fetchall():
        d = dict(r)
        reqs_by_task.setdefault(d["task_local_id"], []).append(d)
    for t in tasks:
        t["depends_on"] = deps_by_task.get(t["local_id"], [])
        t["resource_requirements"] = reqs_by_task.get(t["local_id"], [])
    row["tasks"] = tasks
    return row


def get_template(template_id: str) -> Optional[dict]:
    row = conn().execute(
        "SELECT * FROM lookahead_templates WHERE id = ?", (template_id,)
    ).fetchone()
    return _template_with_tasks(_row_to_dict(row)) if row else None


def list_templates(owner: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM lookahead_templates"
    args: list = []
    if owner:
        sql += " WHERE owner = ?"
        args.append(owner)
    sql += " ORDER BY name, id"
    rows = _rows_to_list(conn().execute(sql, args).fetchall())
    return [_template_with_tasks(r) for r in rows]


def _write_template_tasks(template_id: str, tasks: list[dict]) -> None:
    """Replace all tasks + deps + resource requirements for a template."""
    c = conn()
    c.execute("DELETE FROM lookahead_template_task_resources WHERE template_id = ?",
              (template_id,))
    c.execute("DELETE FROM lookahead_template_task_deps WHERE template_id = ?",
              (template_id,))
    c.execute("DELETE FROM lookahead_template_tasks WHERE template_id = ?",
              (template_id,))
    for t in tasks or []:
        local_id = str(t.get("local_id") or "").strip()
        if not local_id:
            continue
        c.execute(
            "INSERT INTO lookahead_template_tasks "
            "(template_id, local_id, title, offset_start_days, offset_start_shift, "
            " duration_shifts, shift_preference, work_days, linked_procedure_doc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (template_id, local_id,
             (t.get("title") or "").strip(),
             int(t.get("offset_start_days", 0)),
             int(t.get("offset_start_shift", 1)),
             max(1, int(t.get("duration_shifts", 1))),
             int(t.get("shift_preference", 0)),
             (t.get("work_days") or "").strip(),
             (t.get("linked_procedure_doc") or "").strip()),
        )
        for dep in t.get("depends_on") or []:
            dep = str(dep).strip()
            if not dep or dep == local_id:
                continue
            c.execute(
                "INSERT OR IGNORE INTO lookahead_template_task_deps "
                "(template_id, task_local_id, depends_on_local_id) VALUES (?, ?, ?)",
                (template_id, local_id, dep),
            )
        for req in t.get("resource_requirements") or []:
            rtype = (req.get("resource_type") or "").strip()
            role  = (req.get("role") or "").strip()
            named = req.get("named_resource_id")
            # At least one of rtype/role/named must be present.
            if not rtype and not role and named in (None, ""):
                continue
            c.execute(
                "INSERT INTO lookahead_template_task_resources "
                "(template_id, task_local_id, resource_type, role, named_resource_id, quantity) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (template_id, local_id, rtype, role,
                 int(named) if named not in (None, "") else None,
                 float(req.get("quantity", 1))),
            )


def create_template(data: dict) -> dict:
    """Create a new template. Expects ``id`` set by caller (UUID)."""
    tid = data["id"]
    now = _now_iso()
    unit = data.get("duration_unit", "calendar_days")
    if unit not in _DURATION_UNITS:
        raise ValueError(f"invalid duration_unit: {unit}")
    c = conn()
    c.execute(
        "INSERT INTO lookahead_templates "
        "(id, name, description, owner, version, duration_unit, default_project_tag, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (tid,
         (data.get("name") or "").strip(),
         (data.get("description") or "").strip(),
         (data.get("owner") or "").strip(),
         unit,
         (data.get("default_project_tag") or "").strip(),
         now, now),
    )
    _write_template_tasks(tid, data.get("tasks") or [])
    return get_template(tid)


def update_template(template_id: str, updates: dict) -> Optional[dict]:
    """Apply partial update. Bumps ``version`` unconditionally."""
    current = get_template(template_id)
    if not current:
        return None
    c = conn()
    fields = {}
    for k in ("name", "description", "owner", "duration_unit", "default_project_tag"):
        if k in updates:
            fields[k] = (updates[k] or "").strip() if isinstance(updates[k], str) else updates[k]
    if "duration_unit" in fields and fields["duration_unit"] not in _DURATION_UNITS:
        raise ValueError(f"invalid duration_unit: {fields['duration_unit']}")
    fields["version"] = int(current["version"]) + 1
    fields["updated_at"] = _now_iso()
    set_clause = ", ".join(f'"{k}" = ?' for k in fields)
    c.execute(
        f"UPDATE lookahead_templates SET {set_clause} WHERE id = ?",
        list(fields.values()) + [template_id],
    )
    if "tasks" in updates:
        _write_template_tasks(template_id, updates["tasks"] or [])
    return get_template(template_id)


def delete_template(template_id: str) -> None:
    conn().execute("DELETE FROM lookahead_templates WHERE id = ?", (template_id,))


# ── Template instantiation ────────────────────────────────────────────────────

# DOW tokens match project_shifts.days format: "M,T,W,Th,F,Sa,Su".
# Python's date.weekday(): 0=Mon ... 6=Sun.
_DOW_TOKEN_TO_WEEKDAY = {"M": 0, "T": 1, "W": 2, "Th": 3, "F": 4, "Sa": 5, "Su": 6}
_WORKWEEK_BUSINESS    = frozenset({0, 1, 2, 3, 4})  # Mon-Fri


def _parse_work_days(mask: str) -> frozenset[int]:
    """Parse a comma-separated DOW mask into a set of Python weekday ints.

    Empty / whitespace-only / ``None`` returns an empty set meaning "all seven
    days are work days" (caller must treat empty as unrestricted).
    """
    if not mask:
        return frozenset()
    out: set[int] = set()
    for tok in mask.split(","):
        tok = tok.strip()
        if tok in _DOW_TOKEN_TO_WEEKDAY:
            out.add(_DOW_TOKEN_TO_WEEKDAY[tok])
    return frozenset(out)


def _resolve_workweek(unit: str, work_days: str) -> frozenset[int]:
    """Return the effective work-day set for a (unit, work_days) pair.

    Precedence:
      - non-empty ``work_days`` mask wins (per-task/card override from #73).
      - otherwise ``unit == 'business_days'`` falls back to Mon-Fri.
      - else an empty set, meaning "all seven days" (no restriction).
    """
    mask = _parse_work_days(work_days)
    if mask:
        return mask
    if unit == "business_days":
        return _WORKWEEK_BUSINESS
    return frozenset()


def _add_days(date_str: str, n: int, unit: str, work_days: str = "") -> str:
    """Add N days to an ISO date string under the given workweek.

    When the resolved workweek is non-empty and not all seven days, N counts
    only work days (weekends or other off-days are skipped).
    """
    from datetime import date, timedelta
    y, m, d = (int(x) for x in date_str.split("-"))
    cur = date(y, m, d)
    wd = _resolve_workweek(unit, work_days)
    restricted = bool(wd) and len(wd) < 7
    if restricted:
        step = 1 if n >= 0 else -1
        remaining = abs(n)
        while remaining > 0:
            cur = cur + timedelta(days=step)
            if cur.weekday() in wd:
                remaining -= 1
    else:
        cur = cur + timedelta(days=n)
    return cur.isoformat()


def _duration_to_end(start_date: str, start_shift: int, duration_shifts: int,
                     unit: str, work_days: str = "") -> tuple[str, int]:
    """Convert a (start_date, start_shift, duration) triple into (end_date, end_shift)."""
    total_shifts = max(1, int(duration_shifts))
    # Each day holds up to 3 shifts.  Shifts past 3 wrap to the next day.
    end_shift = int(start_shift) + total_shifts - 1
    days_added = 0
    while end_shift > 3:
        end_shift -= 3
        days_added += 1
    end_date = _add_days(start_date, days_added, unit, work_days) if days_added else start_date
    return end_date, end_shift


def instantiate_template(template_id: str, start_date: str,
                         project_tag: str, owner: str = "") -> Optional[dict]:
    """Materialise a template instance.  Returns ``{instance, cards}``."""
    import uuid as _uuid
    tpl = get_template(template_id)
    if not tpl:
        return None
    instance_id = str(_uuid.uuid4())
    now = _now_iso()
    unit = tpl["duration_unit"]
    project = (project_tag or tpl.get("default_project_tag") or "").strip()
    c = conn()
    c.execute(
        "INSERT INTO lookahead_template_instances "
        "(id, template_id, template_version, start_date, project_tag, owner, "
        " status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
        (instance_id, template_id, int(tpl["version"]), start_date, project,
         (owner or tpl.get("owner") or "").strip(), now),
    )
    # Pass 1: create every card with computed dates.
    local_to_card: dict[str, str] = {}
    for task in tpl["tasks"]:
        card_id = str(_uuid.uuid4())
        local_to_card[task["local_id"]] = card_id
        # Per-task work_days mask (parsival#73) overrides the template's
        # duration_unit for this task's date math and propagates to the card.
        task_wd = (task.get("work_days") or "").strip()
        s_date  = _add_days(start_date, int(task["offset_start_days"]), unit, task_wd)
        s_shift = int(task["offset_start_shift"]) or 1
        e_date, e_shift = _duration_to_end(
            s_date, s_shift, int(task["duration_shifts"]), unit, task_wd)
        c.execute(
            "INSERT INTO lookahead_cards "
            "(id, title, project, assignee, start_date, start_shift_num, "
            " end_date, end_shift_num, status, notes, work_days, "
            " linked_procedure_doc, template_instance_id, template_task_local_id, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', '', ?, ?, ?, ?, ?, ?)",
            (card_id, task["title"], project, "",
             s_date, s_shift, e_date, e_shift,
             task_wd,
             task.get("linked_procedure_doc", ""),
             instance_id, task["local_id"], now, now),
        )
        # Copy resource requirements to BOM entries (named_resource_id only).
        for req in task["resource_requirements"]:
            named = req.get("named_resource_id")
            if named:
                c.execute(
                    "INSERT OR IGNORE INTO lookahead_card_resources "
                    "(card_id, resource_id, quantity, status) VALUES (?, ?, ?, 'needed')",
                    (card_id, int(named), float(req.get("quantity", 1))),
                )
    # Pass 2: translate template-local deps to concrete card deps.
    for task in tpl["tasks"]:
        cid = local_to_card[task["local_id"]]
        for dep_local in task["depends_on"]:
            dep_cid = local_to_card.get(dep_local)
            if dep_cid:
                c.execute(
                    "INSERT OR IGNORE INTO lookahead_card_deps "
                    "(card_id, depends_on_id) VALUES (?, ?)",
                    (cid, dep_cid),
                )
    return get_instance(instance_id)


def _annotate_instance_outdated(inst: dict) -> dict:
    """Add ``template_current_version`` and ``outdated`` to an instance row.

    The instance carries the template version it was *materialised* against;
    the template's own ``version`` rises every time the user edits the template.
    Surfacing the gap lets the UI offer an opt-in upgrade per parsival#60.
    """
    if not inst:
        return inst
    cur = conn().execute(
        "SELECT version FROM lookahead_templates WHERE id = ?",
        (inst["template_id"],),
    ).fetchone()
    current = int(cur["version"]) if cur else int(inst.get("template_version") or 1)
    inst["template_current_version"] = current
    inst["outdated"] = current > int(inst.get("template_version") or 0)
    return inst


def get_instance(instance_id: str) -> Optional[dict]:
    row = conn().execute(
        "SELECT * FROM lookahead_template_instances WHERE id = ?", (instance_id,)
    ).fetchone()
    if not row:
        return None
    inst = dict(row)
    cards = list_lookahead_cards_for_instance(instance_id)
    inst["cards"] = cards
    return _annotate_instance_outdated(inst)


def list_instances(project: Optional[str] = None,
                   status: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM lookahead_template_instances"
    args: list = []
    where: list[str] = []
    if project:
        where.append("project_tag = ?")
        args.append(project)
    if status:
        where.append("status = ?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY start_date DESC, id"
    rows = _rows_to_list(conn().execute(sql, args).fetchall())
    return [_annotate_instance_outdated(r) for r in rows]


def list_lookahead_cards_for_instance(instance_id: str) -> list[dict]:
    rows = _rows_to_list(conn().execute(
        "SELECT * FROM lookahead_cards WHERE template_instance_id = ? "
        "ORDER BY start_date, start_shift_num, id",
        (instance_id,),
    ).fetchall())
    return [_card_with_relations(r) for r in rows]


def reschedule_instance(instance_id: str, new_start_date: str) -> Optional[dict]:
    """Shift all cards attached to the instance by (new_start_date - old_start_date).

    Uses the instance's ``duration_unit`` via its template to honour
    business-days vs calendar-days semantics.  Cards whose
    ``template_instance_id`` has been nulled (detached) are untouched.
    """
    c = conn()
    inst = c.execute(
        "SELECT i.*, t.duration_unit FROM lookahead_template_instances i "
        "JOIN lookahead_templates t ON t.id = i.template_id "
        "WHERE i.id = ?", (instance_id,),
    ).fetchone()
    if not inst:
        return None
    inst = dict(inst)
    old_start = inst["start_date"]
    unit = inst["duration_unit"]
    # Delta in calendar days — we apply it per-card using the same unit so
    # business-day templates stay business-day aligned.
    from datetime import date
    def _as_date(s):
        y, m, d = (int(x) for x in s.split("-"))
        return date(y, m, d)
    delta_calendar = (_as_date(new_start_date) - _as_date(old_start)).days
    if delta_calendar == 0:
        return get_instance(instance_id)
    # Count work-days between the two instance anchors, once per distinct
    # effective workweek that any card in the instance uses. Cards with an
    # empty/all-7-days workweek shift by calendar delta; restricted workweeks
    # shift by their own work-day count so relative spacing stays intact.
    from datetime import timedelta
    a, b = sorted([_as_date(old_start), _as_date(new_start_date)])

    def _count_work_days(wd: frozenset[int]) -> int:
        if not wd or len(wd) >= 7:
            return delta_calendar
        n = 0
        cur = a
        while cur < b:
            cur += timedelta(days=1)
            if cur.weekday() in wd:
                n += 1
        return n if delta_calendar > 0 else -n

    now = _now_iso()
    cards = list_lookahead_cards_for_instance(instance_id)
    workweek_cache: dict[frozenset[int], int] = {}
    for card in cards:
        wd = _resolve_workweek(unit, card.get("work_days") or "")
        if wd not in workweek_cache:
            workweek_cache[wd] = _count_work_days(wd)
        card_delta = workweek_cache[wd]
        card_unit  = "business_days" if (wd and len(wd) < 7) else unit
        card_wd    = card.get("work_days") or ""
        new_s = _add_days(card["start_date"], card_delta, card_unit, card_wd)
        new_e = _add_days(card["end_date"],   card_delta, card_unit, card_wd)
        c.execute(
            "UPDATE lookahead_cards SET start_date = ?, end_date = ?, updated_at = ? "
            "WHERE id = ?",
            (new_s, new_e, now, card["id"]),
        )
    c.execute(
        "UPDATE lookahead_template_instances SET start_date = ? WHERE id = ?",
        (new_start_date, instance_id),
    )
    return get_instance(instance_id)


def upgrade_instance(instance_id: str) -> Optional[dict]:
    """Re-apply the current template version to an existing instance.

    Per parsival#60: the user opted in to this upgrade.  Existing cards keep
    their assignee, status, notes, and any user-added BOM entries; the template-
    derived fields (title, schedule offsets, linked procedure doc, required
    named resources) get refreshed.  Tasks added since the original
    instantiation become new cards.  Tasks that were removed from the template
    leave their existing cards alone — they may already be in flight.
    Dependencies are rebuilt from the current template graph.
    """
    import uuid as _uuid
    c = conn()
    inst_row = c.execute(
        "SELECT * FROM lookahead_template_instances WHERE id = ?", (instance_id,)
    ).fetchone()
    if not inst_row:
        return None
    inst = dict(inst_row)
    tpl = get_template(inst["template_id"])
    if not tpl:
        return None
    if int(inst["template_version"]) >= int(tpl["version"]):
        return get_instance(instance_id)  # already current

    start_date = inst["start_date"]
    project    = inst["project_tag"]
    unit       = tpl["duration_unit"]
    now        = _now_iso()

    existing = _rows_to_list(c.execute(
        "SELECT * FROM lookahead_cards WHERE template_instance_id = ?",
        (instance_id,),
    ).fetchall())
    card_by_local: dict[str, dict] = {
        r["template_task_local_id"]: r for r in existing if r["template_task_local_id"]
    }

    # Pass 1: update or create one card per template task.
    local_to_card_id: dict[str, str] = {}
    for task in tpl["tasks"]:
        local_id = task["local_id"]
        task_wd = (task.get("work_days") or "").strip()
        s_date  = _add_days(start_date, int(task["offset_start_days"]), unit, task_wd)
        s_shift = int(task["offset_start_shift"]) or 1
        e_date, e_shift = _duration_to_end(
            s_date, s_shift, int(task["duration_shifts"]), unit, task_wd)
        existing_card = card_by_local.get(local_id)
        if existing_card:
            cid = existing_card["id"]
            c.execute(
                "UPDATE lookahead_cards SET title = ?, start_date = ?, "
                "start_shift_num = ?, end_date = ?, end_shift_num = ?, "
                "work_days = ?, linked_procedure_doc = ?, updated_at = ? "
                "WHERE id = ?",
                (task["title"], s_date, s_shift, e_date, e_shift,
                 task_wd,
                 task.get("linked_procedure_doc", ""), now, cid),
            )
        else:
            cid = str(_uuid.uuid4())
            c.execute(
                "INSERT INTO lookahead_cards "
                "(id, title, project, assignee, start_date, start_shift_num, "
                " end_date, end_shift_num, status, notes, work_days, "
                " linked_procedure_doc, template_instance_id, template_task_local_id, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', '', ?, ?, ?, ?, ?, ?)",
                (cid, task["title"], project, "",
                 s_date, s_shift, e_date, e_shift,
                 task_wd,
                 task.get("linked_procedure_doc", ""),
                 instance_id, local_id, now, now),
            )
        local_to_card_id[local_id] = cid

        # BOM: add any required named resource that isn't already on the card.
        # We never remove existing entries — the user may have flipped a row to
        # 'consumed' or added their own.
        for req in task["resource_requirements"]:
            named = req.get("named_resource_id")
            if named:
                c.execute(
                    "INSERT OR IGNORE INTO lookahead_card_resources "
                    "(card_id, resource_id, quantity, status) VALUES (?, ?, ?, 'needed')",
                    (cid, int(named), float(req.get("quantity", 1))),
                )

    # Pass 2: rebuild deps for cards that map to the current template.
    # Deps for cards whose local_id was dropped from the template stay alone;
    # those cards are no longer part of the template graph.
    for cid in local_to_card_id.values():
        c.execute(
            "DELETE FROM lookahead_card_deps WHERE card_id = ?", (cid,)
        )
    for task in tpl["tasks"]:
        cid = local_to_card_id[task["local_id"]]
        for dep_local in task["depends_on"]:
            dep_cid = local_to_card_id.get(dep_local)
            if dep_cid:
                c.execute(
                    "INSERT OR IGNORE INTO lookahead_card_deps "
                    "(card_id, depends_on_id) VALUES (?, ?)",
                    (cid, dep_cid),
                )

    c.execute(
        "UPDATE lookahead_template_instances SET template_version = ? WHERE id = ?",
        (int(tpl["version"]), instance_id),
    )
    return get_instance(instance_id)


def set_instance_status(instance_id: str, status: str) -> Optional[dict]:
    if status not in _INSTANCE_STATUSES:
        raise ValueError(f"invalid instance status: {status}")
    conn().execute(
        "UPDATE lookahead_template_instances SET status = ? WHERE id = ?",
        (status, instance_id),
    )
    return get_instance(instance_id)


def delete_instance(instance_id: str) -> None:
    """Delete an instance and its still-attached cards.  Detached cards stay."""
    c = conn()
    c.execute(
        "DELETE FROM lookahead_cards WHERE template_instance_id = ?", (instance_id,)
    )
    c.execute(
        "DELETE FROM lookahead_template_instances WHERE id = ?", (instance_id,)
    )


def detach_card(card_id: str) -> Optional[dict]:
    """Remove the card from its template instance without deleting it."""
    conn().execute(
        "UPDATE lookahead_cards SET template_instance_id = NULL, "
        "template_task_local_id = NULL WHERE id = ?",
        (card_id,),
    )
    return get_lookahead_card(card_id)


def maybe_autocomplete_instance(instance_id: str) -> None:
    """Flip instance to ``complete`` if every attached card is done."""
    if not instance_id:
        return
    c = conn()
    inst = c.execute(
        "SELECT status FROM lookahead_template_instances WHERE id = ?", (instance_id,)
    ).fetchone()
    if not inst or inst["status"] != "active":
        return
    row = c.execute(
        "SELECT COUNT(*) AS n_total, "
        "       SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS n_done "
        "FROM lookahead_cards WHERE template_instance_id = ?",
        (instance_id,),
    ).fetchone()
    if row and row["n_total"] and row["n_total"] == row["n_done"]:
        c.execute(
            "UPDATE lookahead_template_instances SET status = 'complete' WHERE id = ?",
            (instance_id,),
        )


# ── Cross-system link suggestions (parsival#50) ───────────────────────────────
#
# LLM annotators write proposed card↔item links into this pool; the user
# accepts or rejects each one.  Accepted suggestions graduate to the concrete
# ``lookahead_card_links`` table and stay marked ``decision='accepted'`` so
# the annotator doesn't re-propose them.

def list_card_suggestions(card_id: str,
                          include_decided: bool = False) -> list[dict]:
    """Return suggestions for a card.  Pending only by default."""
    sql = ("SELECT * FROM lookahead_card_link_suggestions WHERE card_id = ?")
    args: list = [card_id]
    if not include_decided:
        sql += " AND decision IS NULL"
    sql += " ORDER BY id"
    return _rows_to_list(conn().execute(sql, args).fetchall())


def add_card_suggestion(card_id: str, link_type: str, target_id: str,
                        reason: str = "") -> Optional[dict]:
    """Insert a new pending suggestion.  Deduped on (card, type, target)."""
    if link_type not in _LINK_TYPES:
        return None
    target_id = str(target_id)
    c = conn()
    # Skip if already proposed (pending or decided) for this card+target.
    existing = c.execute(
        "SELECT id FROM lookahead_card_link_suggestions "
        "WHERE card_id = ? AND link_type = ? AND target_id = ?",
        (card_id, link_type, target_id),
    ).fetchone()
    if existing:
        return None
    cur = c.execute(
        "INSERT INTO lookahead_card_link_suggestions "
        "(card_id, link_type, target_id, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (card_id, link_type, target_id, reason, _now_iso()),
    )
    row = c.execute(
        "SELECT * FROM lookahead_card_link_suggestions WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return _row_to_dict(row)


def decide_card_suggestion(suggestion_id: int, decision: str) -> Optional[dict]:
    """Accept or reject a suggestion.  Accepted ones also become card_links."""
    if decision not in ("accepted", "rejected"):
        raise ValueError(f"invalid decision: {decision}")
    c = conn()
    row = c.execute(
        "SELECT * FROM lookahead_card_link_suggestions WHERE id = ?",
        (int(suggestion_id),),
    ).fetchone()
    if not row:
        return None
    c.execute(
        "UPDATE lookahead_card_link_suggestions "
        "SET decision = ?, decided_at = ? WHERE id = ?",
        (decision, _now_iso(), int(suggestion_id)),
    )
    if decision == "accepted":
        c.execute(
            "INSERT OR IGNORE INTO lookahead_card_links "
            "(card_id, link_type, target_id) VALUES (?, ?, ?)",
            (row["card_id"], row["link_type"], row["target_id"]),
        )
    return _row_to_dict(c.execute(
        "SELECT * FROM lookahead_card_link_suggestions WHERE id = ?",
        (int(suggestion_id),),
    ).fetchone())


# ── Slack scan dedup (parsival#69) ────────────────────────────────────────────

def slack_unseen_message_ts(team: str, channel_id: str,
                            ts_list: list[str]) -> set[str]:
    """
    Return the subset of ``ts_list`` that has not yet been recorded as seen
    for the given ``(team, channel_id)``.

    Used by ``connector_slack._fetch_for_token`` to keep only newly-surfaced
    messages in each scan.  The connector calls this to filter the raw
    channel/DM/mention message list before building a ``RawItem``; if the
    filtered list is empty no item is emitted.

    :param team: Slack workspace name, or ``""`` for the legacy bot path.
    :param channel_id: Slack channel ID (``C...``, ``D...``, or ``G...``).
    :param ts_list: Message timestamps to test (native Slack ``ts`` strings).
    :return: Set of ts strings that are not yet in ``slack_seen_messages``.
    """
    if not ts_list:
        return set()
    c = conn()
    placeholders = ",".join(["?"] * len(ts_list))
    rows = c.execute(
        f"SELECT ts FROM slack_seen_messages WHERE team = ? AND channel_id = ? "
        f"AND ts IN ({placeholders})",
        (team, channel_id, *ts_list),
    ).fetchall()
    seen = {row["ts"] for row in rows}
    return {ts for ts in ts_list if ts not in seen}


def slack_mark_messages_seen(team: str, channel_id: str,
                             ts_list: list[str]) -> None:
    """
    Record ``(team, channel_id, ts)`` tuples as already surfaced.

    Called by the Slack connector right after it emits a ``RawItem`` built
    from ``ts_list`` so subsequent scans can skip those messages.  Uses
    ``INSERT OR IGNORE`` to stay idempotent — re-calling with the same
    timestamps is a no-op.
    """
    if not ts_list:
        return
    now = _now_iso()
    c = conn()
    c.executemany(
        "INSERT OR IGNORE INTO slack_seen_messages "
        "(team, channel_id, ts, seen_at) VALUES (?, ?, ?, ?)",
        [(team, channel_id, ts, now) for ts in ts_list],
    )


def candidate_items_for_card(project: str, start_date: str, end_date: str,
                             limit: int = 40) -> list[dict]:
    """Return items tagged to the same project whose timestamp sits near the
    card window.  Used as the shortlist the LLM ranks against the card.
    """
    if not project:
        return []
    # Widen the window by two weeks on each side — correlations aren't always
    # same-week, and the LLM can still reject out-of-scope candidates.
    from datetime import date, timedelta
    def _as_date(s):
        y, m, d = (int(x) for x in s.split("-"))
        return date(y, m, d)
    try:
        lo = (_as_date(start_date) - timedelta(days=14)).isoformat()
        hi = (_as_date(end_date)   + timedelta(days=14)).isoformat()
    except Exception:
        lo, hi = "", "9999"
    items = get_items_by_project(project)
    pruned = [i for i in items
              if lo <= (i.get("timestamp", "")[:10] or "") <= hi]
    pruned.sort(key=lambda i: i.get("timestamp") or "", reverse=True)
    return pruned[:limit]
