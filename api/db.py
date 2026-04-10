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
