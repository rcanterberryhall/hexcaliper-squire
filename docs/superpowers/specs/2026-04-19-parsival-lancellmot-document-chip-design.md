# Parsival → lancellmot Document Chip (parsival#43)

**Date:** 2026-04-19
**Issue:** [parsival#43](https://github.com/rcanterberryhall/hexcaliper-parsival/issues/43)
**Scope:** Ship a visible cross-system link from parsival situations to lancellmot documents, all in one PR. Future work (parsival#50 and similar) adopts the alias primitive when it's built.

---

## The story

A situation card in parsival gets a small chip next to the project tag. Hovering it shows the top documents from the matching lancellmot project; clicking a filename opens lancellmot's doc viewer in a new tab. If the project isn't mapped yet, the chip renders dim with a pencil icon — clicking it jumps to Settings with the relevant project row focused, where the user picks the lancellmot counterpart from a dropdown. If lancellmot is unreachable, the chip renders amber with a "retry" tooltip instead of silently hiding.

No fuzzy matching. Aliases are explicit and user-maintained — the friction of hand-mapping is the forcing function that keeps naming discipline visible.

No caching. Chips fetch per-render. Situation-only scope keeps N small.

## Design decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Identity resolution | Strict alias table, no fallback | Explicit, debuggable, enforces naming discipline |
| Chip surface | Situation cards only (not items) | Situations are the workflow surface; items are too numerous |
| Chip payload | Popover: project name + top N filenames + link out | Filenames give context; links avoid embedding lancellmot |
| Fetch strategy | Per-render, no cache | Situations-per-page is small; freshness trivial |
| Multi-project situations | One chip per resolved project | Preserves project boundaries; chips wrap if needed |
| Unresolved tag | Muted "Map →" chip | Discoverable at point-of-use; one click to bootstrap |
| Failure mode | Amber "unreachable" chip with retry tooltip | User's stated preference: fail loud, not silent |
| Alias UI | New column on existing Settings → Projects row | Alias is an attribute of the project; dropdown prevents typos |

## Architecture

### Data model

New table in parsival's SQLite:

```sql
CREATE TABLE lancellmot_aliases (
    parsival_project TEXT PRIMARY KEY,  -- matches parsival project name (from config.projects)
    lancellmot_project_id TEXT NOT NULL,
    lancellmot_project_name TEXT NOT NULL,  -- denormalized for display/audit
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

- `parsival_project` is the exact name as stored in `settings.projects[].name`.
- `lancellmot_project_id` is the UUID/id returned by lancellmot's `/workspace/projects` endpoint.
- `lancellmot_project_name` is denormalized so audit works even if lancellmot is down.

### HTTP client

New module `api/lancellmot_client.py` with three functions:

```python
def list_projects() -> list[dict]:
    """GET {LANCELLMOT_URL}/workspace/projects → [{id, name, ...}, ...]"""

def list_documents(project_id: str, limit: int = 5) -> list[dict]:
    """GET {LANCELLMOT_URL}/documents?project_id=X → [{id, filename, ...}, ...], trimmed"""

class LancellmotUnavailable(Exception):
    """Raised on network error, timeout, or non-2xx response."""
```

- Timeout: 2s per call (fast enough to not stall card renders; long enough for local network).
- No retry inside the client — failure bubbles to the endpoint handler, which returns a shape the UI can render as the amber chip.
- Configured via `LANCELLMOT_URL` env var (pattern matches existing `MERLLM_URL`).

### Parsival-side API

New routes:

```
GET  /api/lancellmot/projects
     → proxy to lancellmot's list; used to populate Settings dropdown.
     → on failure: 503 with {error: "unreachable"}

GET  /api/lancellmot/docs-for-tag?tag=Ethylene-Cracker-3&limit=5
     → resolve tag via aliases table; if resolved, fetch docs from lancellmot.
     → returns: {status: "ok", project_name, project_id, docs: [...]}
                | {status: "unmapped", tag}
                | {status: "unreachable", tag}

GET  /api/lancellmot/aliases
     → list all aliases for Settings audit.

PUT  /api/lancellmot/aliases
     body: {parsival_project, lancellmot_project_id, lancellmot_project_name}
     → upsert alias.

DELETE /api/lancellmot/aliases/{parsival_project}
     → remove alias.
```

### UI changes

1. **Situation card render** — for each project tag on the situation, render a chip. State depends on `GET /api/lancellmot/docs-for-tag` response:
   - `ok` → active chip with doc count; popover on hover.
   - `unmapped` → muted chip with pencil icon; click opens Settings.
   - `unreachable` → amber chip; tooltip with retry hint.

2. **Settings → Projects section** — add a new column "lancellmot project" to each project row. Dropdown populated from `GET /api/lancellmot/projects`, with current alias selected (or blank). On change, call `PUT /api/lancellmot/aliases`.

3. **Click-to-edit from card** — unmapped chip opens Settings modal, scrolls to Projects section, focuses the row whose name matches the tag.

## Error handling

- lancellmot unreachable on chip fetch → amber chip, no retry inside the client.
- lancellmot unreachable on Settings open → dropdown shows empty with an inline message "couldn't reach lancellmot — close and retry."
- Alias points to a lancellmot project that no longer exists → lancellmot returns 404 for the doc list → chip renders as unreachable (pragmatic: the user can fix it on their next Settings visit).

## Testing

- **api/lancellmot_client.py**: unit tests with mocked `httpx` responses (success, timeout, 5xx, 404).
- **Aliases CRUD**: round-trip upsert/list/delete against the real SQLite test db.
- **docs-for-tag endpoint**: integration tests covering mapped/unmapped/unreachable paths.
- **UI**: smoke test via playwright or the existing manual checklist — verify the three chip states render.

## Out of scope (for this PR)

- Bi-directional linking (lancellmot → parsival).
- Situation-level primary-project precedence rules.
- Background alias sync / auto-discovery.
- Item-level chips.
- Caching.

## Follow-ups

- parsival#50 adopts the alias primitive when it's built.
- First real-use iteration informs refinements: chip density, popover N value, bootstrap friction. Expected to change based on usage.
