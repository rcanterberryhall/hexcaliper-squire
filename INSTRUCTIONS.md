# Parsival — User Guide

## Table of Contents

1. [First-time setup](#1-first-time-setup)
2. [The seed workflow](#2-the-seed-workflow)
3. [Connecting sources](#3-connecting-sources)
4. [Running a scan](#4-running-a-scan)
5. [Scheduled auto-scans](#5-scheduled-auto-scans)
6. [Understanding the dashboard](#6-understanding-the-dashboard)
7. [Managing todos](#7-managing-todos)
8. [Reviewing analyses](#8-reviewing-analyses)
9. [Pre-scan noise filters](#9-pre-scan-noise-filters)
10. [Situations](#10-situations)
11. [Adaptive attention model](#11-adaptive-attention-model)
12. [Intel](#12-intel)
13. [Project configuration](#13-project-configuration)
14. [Settings reference](#14-settings-reference)
15. [Outlook sidecar (Windows)](#15-outlook-sidecar-windows)
16. [Thunderbird sidecar (Ubuntu)](#16-thunderbird-sidecar-ubuntu)
17. [Re-analysis](#17-re-analysis)
18. [Maintenance](#18-maintenance)

---

## 1. First-time setup

### Deploy the stack

```bash
git clone <repo> hexcaliper-parsival
cd hexcaliper-parsival
```

Edit `docker-compose.yml`. Search for `your-` to find every placeholder:

| Placeholder | What to fill in |
|-------------|-----------------|
| `your-ollama-url` | Full URL to your Ollama endpoint, e.g. `https://ollama.hexcaliper.com/api/generate` |
| `your-cf-client-id` | Cloudflare Access service token Client ID |
| `your-cf-client-secret` | Cloudflare Access service token Client Secret |
| `your-name` | Your display name, e.g. `Jane Smith` |
| `your-email` | Your email address |

Optional but recommended: set `CREDENTIALS_KEY` to a strong random value (e.g. a UUID4) to encrypt OAuth tokens at rest in SQLite.

Everything else (Slack, GitHub, Jira, etc.) can be left blank initially and filled in later via the Settings page.

```bash
docker compose up --build -d
open http://localhost:8082/page/
```

The stack is ready when the browser loads the dashboard.

### Fix data directory permissions (first run only)

Docker creates `data/` as root on the first run. If you need to run host-side scripts (migration, sidecars) before bringing the stack up, fix ownership first:

```bash
docker compose down
sudo chown -R $(whoami):$(whoami) data/
```

---

## 2. The seed workflow

The seed workflow is the recommended starting point when you have existing email, Slack, or GitHub data you want Parsival to learn from. It discovers your projects automatically rather than requiring you to configure them manually upfront.

### Step 1 — Ingest existing data

Before starting the seed, push some historical data into Parsival:

- **Outlook**: run the sidecar in seed mode (fetches last 30 days):
  ```bash
  python scripts/outlook_sidecar.py --seed
  ```
- **Other sources**: use the Settings page to add credentials, then run a manual scan.

### Step 2 — Start the seed job

Click **Seed** in the navigation or call `POST /seed` with an optional context string:

```json
{
  "context": "I work on platform infrastructure. Key projects are cloud migration, cost reduction, and on-call tooling."
}
```

The context is passed to the LLM and helps it disambiguate similar project names. You can update it while the job is running via `PATCH /seed/context`.

### Step 3 — Review proposals

When the state reaches `review`, the UI displays the LLM's proposed project list and focus topics. Edit names, merge duplicates, and delete anything irrelevant before confirming.

### Step 4 — Apply

Click **Apply** (`POST /seed/apply`). Parsival will:
1. Save the confirmed projects and topics to settings.
2. Re-tag all stored items by keyword match.
3. Build semantic embeddings for each project.
4. Re-analyze all items with the full context.

### Step 5 — Scan (optional)

After re-analysis completes, the UI prompts you to run a live scan to pull fresh items. Click **Scan** or **Skip** to go straight to the dashboard.

---

## 3. Connecting sources

### GitHub

1. Go to **Settings → GitHub**.
2. Paste a PAT with `repo` and `notifications` scopes.
3. Enter your GitHub username.
4. Save.

### Jira

1. Go to **Settings → Jira**.
2. Enter your Atlassian email, an API token from `id.atlassian.com/manage-profile/security/api-tokens`, and your domain (e.g. `yourco.atlassian.net`).
3. Optionally customise the JQL query (default: `assignee = currentUser() AND statusCategory != Done`).
4. Save.

### Slack

1. Go to **Settings → Slack** and click **Connect Slack workspace**.
2. Authorise the OAuth flow in your browser.
3. Repeat for additional workspaces.

Required OAuth user scopes: `channels:history` `channels:read` `groups:history` `groups:read` `im:history` `im:read` `mpim:history` `mpim:read` `search:read` `users:read`

### Microsoft Teams

1. Go to **Settings → Teams** and click **Connect Teams account**.
2. Authorise via the Microsoft identity platform.
3. Repeat for additional tenants.

### Outlook / Thunderbird

See [section 12](#12-outlook-sidecar-windows) and [section 13](#13-thunderbird-sidecar-ubuntu).

---

## 4. Running a scan

Click **Scan** in the top navigation. Select which sources to include and click **Start Scan**.

The scan runs in the background. A status bar shows the current source and item being processed. The dashboard updates live as results arrive.

To cancel mid-scan, click **Stop**. Items already analysed are kept; the scan log records the partial run.

**Tip:** Scans deduplicate by `item_id`, so running the same scan twice is safe.

---

## 5. Scheduled auto-scans

Instead of triggering scans manually, you can schedule each connector to scan on a fixed interval.

### Configuring schedules

Go to **Settings → Schedule**. Each source has an interval dropdown:

| Interval | Meaning |
|----------|---------|
| Off | Manual only (default) |
| 15m / 30m / 1h / 2h / 4h | Scan this source at the selected interval |

Changes take effect immediately — no restart required.

### How auto-scans work

- Auto-scans run the same pipeline as manual scans (same analysis, same graph indexing, same situation formation).
- Each auto-scan runs only for its scheduled source, not all sources at once.
- If a scan is already running when a scheduled scan is due, the scheduled scan is skipped and logged.
- Schedule status is visible in `GET /scan/status` — shows next/last run times per source.

### Recommended setup

For most workflows, schedule Slack and Teams at 15–30 minutes, GitHub at 1 hour, and leave Outlook/Thunderbird on the sidecar cron. Jira can often run at 2–4 hours since ticket updates are less time-sensitive than messages.

---

## 6. Understanding the dashboard

### Item badges

Each item in the Analyses list shows a coloured badge indicating its category:

| Badge | Colour | Meaning |
|-------|--------|---------|
| `task · reply` | Red | You need to compose a reply |
| `task · review` | Blue | You need to read/review something |
| `task` | Orange | General action item |
| `approval` | Purple | Needs your sign-off |
| `fyi` | Grey | Informational only |
| `noise` | Light grey | Filtered out (visible when showing all) |

### Priority

Items are sorted high → medium → low within each category. Priority is assigned by the LLM but can be overridden per-item.

### Hierarchy

The `hierarchy` field indicates how directly the item relates to you:

| Tier | What it means |
|------|---------------|
| `user` | You are in To/CC, @mentioned, or directly assigned |
| `project` | Related to one of your projects |
| `topic` | Matches a watch topic |
| `general` | Catch-all |

### Filters

Use the filter bar above the list to narrow by source, category, priority, or project tag.

---

## 7. Managing todos

### Viewing todos

The **Todos** tab shows all open action items extracted from analysed items, sorted by priority (high → medium → low).

Use query parameters to filter:
- `?done=true` — include completed items
- `?source=slack` — filter by source
- `?priority=high` — filter by priority

### Marking complete

Click the checkbox next to a todo or use the **✓** button. The item moves to the completed list.

### Editing a todo

Click the **✎** (pencil) button on a todo row to edit:
- Description
- Deadline
- Priority
- Project tag

### Creating a manual card

Click **+ New card** at the top of the Todos panel. A blank manual card is created and the detail panel opens for editing: title, summary, body/notes, priority, project tag, goals, key dates, linked tasks, and "why this priority" are all editable inline.

Manual cards are flagged with `source='manual'` and are skipped by re-analysis, so manual content is never overwritten by the LLM.

### Deleting a todo

Click the **✕** button on a todo row. This permanently removes the row.

---

## 8. Reviewing analyses

### Opening an item

Click any row in the Analyses list to open the detail panel. This shows:
- Full summary
- Action items
- Goals and key dates extracted by the LLM
- Intel items (key facts)
- Source metadata (author, To/CC, timestamp)

### Editing an analysis

In the detail panel, click **Edit** to change:

| Field | Notes |
|-------|-------|
| Category | `task`, `approval`, `fyi`, or `noise` |
| Task type | `reply`, `review`, or none (only relevant for `task`) |
| Priority | `high`, `medium`, or `low` |
| Project tag | Must match a configured project name |

Changing `category` to `noise` immediately removes all associated todos and clears `has_action`. Changing `priority` syncs to all associated todo rows.

### Tagging an item to a project

Open the item, click **Tag to project**, and select a project. This:
1. Updates the `project_tag` on the analysis record.
2. Triggers background keyword and sender learning for that project.

On subsequent scans, items from the same sender or matching the learned keywords will be auto-tagged.

### Marking as noise

Click **Mark as noise** on any item. This:
1. Sets `category="noise"`, `priority="low"`, `has_action=false`.
2. Removes associated todos.
3. Triggers background keyword extraction into the noise filter.

Future items matching these keywords will be pre-labelled as noise by the LLM.

---

## 9. Pre-scan noise filters

Pre-scan filters skip known-irrelevant items *before* the LLM runs, saving inference time and reducing noise without waiting for the model.

### Managing filters

Go to **Settings → Filters**. Each filter is a rule with a type and value:

| Rule type | Example | What it skips |
|-----------|---------|---------------|
| `sender_contains` | `noreply@` | Items from matching senders |
| `subject_contains` | `Out of Office` | Items with matching subject/title |
| `source_repo` | `some-repo-i-only-watch` | GitHub notifications from that repo |
| `distribution_list` | `all-company@corp.com` | Emails to that distribution list |

### Creating filters from noise

When you mark an item as noise in the main view, Parsival offers a "Create filter from this?" option that pre-fills a rule based on the item's sender, source, or subject. This is the fastest way to build your filter list.

### What happens to filtered items

Filtered items are stored with status `filtered` — they are auditable (you can see them) but skip the entire LLM pipeline. The Settings → Filters page shows the count of items filtered in the last scan.

---

## 10. Situations

Situations are automatically-formed cross-source clusters. When multiple items relate to the same event or workstream (e.g. a production incident generating Slack messages, GitHub issues, and Jira tickets), Parsival groups them into a single situation with a composite urgency score and a lifecycle workflow.

### Viewing situations

Open the **Situations** tab. The list defaults to showing situations with status **new**, **investigating**, or **waiting**. Situations are sorted by score descending (most urgent first), with overdue follow-ups surfaced at the top with a visual indicator.

Each situation shows:
- LLM-generated title and summary
- Status badge (new / investigating / waiting / resolved / dismissed)
- Composite urgency score
- Follow-up date (if set)
- Contributing sources
- Open action items (union of all constituent todos)

### Status workflow

Each situation has a formal lifecycle. Use the status dropdown on a situation card to transition it:

| Status | When to use |
|--------|-------------|
| `new` | Default — freshly identified by the correlation layer |
| `investigating` | You are actively looking into it |
| `waiting` | Blocked or pending external input. Set a follow-up date to be reminded. |
| `resolved` | Completed — no further action needed |
| `dismissed` | Irrelevant to your work |

Every status change is logged with a timestamp and optional note. View the transition history by clicking the situation's event log.

### Follow-up dates

When a situation is in `waiting` status, set a follow-up date using the date picker. When the date passes, the situation surfaces at the top of the list with a visual indicator so you don't forget about it.

### Filtering situations

Use the filter bar to narrow by `project`, `status`, or `min_score`. Toggle "Show resolved/dismissed" to see completed situations.

### Manually rescoring

Click **Rescore** to trigger immediate score recomputation and LLM re-synthesis for a situation (useful after merging new items or editing constituent analyses).

---

## 11. Adaptive attention model

Parsival learns from your behavior to predict which items deserve your attention. This replaces static priority tiers with a learned attention score — no configuration needed.

### How it works

Every time you interact with an item (open it, tag it, mark it as noise, change a situation status, submit for deep analysis, or create a todo), Parsival records the action. It maintains two embedding centroids:

- **Attended centroid** — the average embedding of items you've interacted with positively (opened, tagged, investigated, deep-analysed).
- **Ignored centroid** — the average embedding of items you never touched within 48 hours, or explicitly marked as noise.

For each new item, an attention score is computed: how similar it is to what you typically care about, minus how similar it is to what you typically ignore.

### What you see

- Items are sorted by attention score by default. You can switch to chronological or LLM-priority sorting via the sort control above the list.
- High-attention items have a subtly bolder border — no numbers or noisy badges.
- The briefing generation weights items by attention score, so high-attention items get more space.

### Cold start

Until you've recorded 50 actions, the system displays "Learning your attention patterns — prioritization will improve as you use the tool" and falls back to the LLM's priority tier for sorting.

### Adaptation over time

The model adapts as your focus shifts. Actions older than 30 days contribute at 50% weight; older than 60 days at 25%. When you're deep in a commissioning week, commissioning-related items float up. When you shift to vendor management, those items rise instead. No settings change needed.

---

## 12. Intel

Intel items are key facts and completed-action notes extracted by the LLM that are worth knowing but are not action items for you — e.g. "Server was rebooted at 03:00", "PR #421 was merged by Alice".

### Viewing intel

Open the **Intel** tab. Filter by source or project tag.

### Dismissing intel

Click **Dismiss** on an intel row to hide it. Unlike situations, dismissed intel is permanently hidden (use `DELETE /intel/{id}` to remove it entirely).

---

## 13. Project configuration

Projects are the core of Parsival's relevance engine. Each project acts as a named workstream that items can be tagged to.

### Adding a project

Go to **Settings → Projects** and add a project object:

```json
{
  "name": "Platform Migration",
  "description": "Kubernetes migration from on-prem to EKS; owned by the platform team.",
  "keywords": ["k8s", "migration", "eks", "node pool"],
  "channels": ["platform-eng", "infra-alerts"],
  "senders": ["platform-team@company.com"],
  "parent": "",
  "learned_keywords": [],
  "learned_senders": []
}
```

| Field | Purpose |
|-------|---------|
| `name` | Exact name used in tagging — the LLM must use this verbatim |
| `description` | Passed to the LLM to disambiguate projects with similar names |
| `keywords` | Manually curated; items containing these are tagged to this project |
| `channels` | Slack/Teams channels monitored for this project |
| `senders` | Email addresses or group aliases associated with this project |
| `parent` | Optional parent project name for sub-project relationships |
| `learned_keywords` | Auto-populated by the tagging workflow — do not edit by hand |
| `learned_senders` | Auto-populated by the tagging workflow — do not edit by hand |

### Sub-projects

Set `parent` to an existing project name to create a hierarchy. The LLM prompt notes the sub-project relationship and will prefer the most specific match.

### Removing a project

Delete the project object from the `PROJECTS` array in Settings and save. Existing items tagged to the project retain their `project_tag` value but will no longer receive learned-keyword updates.

---

## 14. Settings reference

Access via **Settings** in the navigation. All fields can be set here or in `docker-compose.yml`. Changes take effect immediately without a container restart.

### Core

| Field | Description |
|-------|-------------|
| Ollama URL | Full endpoint for your Ollama instance |
| Ollama model | Model name, e.g. `llama3.2`, `mistral:7b` |
| Lookback hours | How many hours of history each scan pulls (default: 48) |

### Identity

| Field | Description |
|-------|-------------|
| Your name | Used in every LLM prompt and in Slack/Teams pre-filtering |
| Your email | Used to identify direct-address items |
| Focus topics | Comma-separated keywords — items matching these get `hierarchy=topic` |

### Analysis Provider

Choose which LLM backend Parsival uses for all analysis, seeding, correlation, and briefing tasks.

| Field | Description |
|-------|-------------|
| Provider | `ollama` (local, default), `ollama_cloud` (paid Ollama endpoint), or `claude` (Anthropic API) |
| Model override | Optional — overrides the default Ollama model. E.g. `llama3:70b` for Ollama Cloud or `claude-sonnet-4-20250514` for Claude |
| API URL | Base URL for Ollama Cloud (e.g. `https://api.ollama.com`). Not needed for local Ollama or Claude |
| API Key | Bearer token for Ollama Cloud, or `x-api-key` for Claude. Not needed for local Ollama |

The provider-specific fields (API URL, API Key) are shown/hidden dynamically based on the selected provider. Changes take effect immediately.

### Schedule

| Field | Description |
|-------|-------------|
| Source intervals | Per-source auto-scan interval (off / 15m / 30m / 1h / 2h / 4h) |

### Noise filters

| Field | Description |
|-------|-------------|
| Filter rules | List of pre-scan rules (sender_contains, subject_contains, source_repo, distribution_list) |

### Secrets

Secrets are displayed masked after first entry. Submitting a masked value (containing `•`) leaves the original intact — you only need to re-enter a secret if you are changing it.

When `CREDENTIALS_KEY` is set, all OAuth tokens (Slack, Teams) are encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256). Without it, tokens are stored in plaintext (backward compatible). A startup warning is logged when encryption is not configured.

| Field | Notes |
|-------|-------|
| CF Access Client ID | Cloudflare Access service token for Ollama |
| CF Access Client Secret | Cloudflare Access service token secret |
| Slack Client ID / Secret | Slack app OAuth credentials |
| Slack Bot Token | Legacy path only — prefer OAuth |
| GitHub PAT | `repo` + `notifications` scopes required |
| Jira Email / Token | Atlassian API token |
| Teams Client ID / Secret | Azure AD app credentials |

---

## 15. Outlook sidecar (Windows)

The Outlook sidecar reads from the local Outlook client via `win32com` and POSTs to Parsival. It must run on the Windows machine where Outlook is installed.

### Install dependencies

```powershell
pip install requests pywin32 keyring
```

### First-time credential setup

```powershell
python scripts\outlook_sidecar.py --setup
```

Enter your Cloudflare Access Client ID and Client Secret when prompted. They are stored in Windows Credential Manager and never written to disk.

### Normal run

```powershell
python scripts\outlook_sidecar.py
```

Fetches the last 48 hours from Inbox and Sent Items and POSTs to Parsival.

### Seed run (historical import)

```powershell
python scripts\outlook_sidecar.py --seed
```

Fetches the last 30 days (up to 500 emails). Run once after initial setup before starting the seed workflow.

### Scheduling

Use Windows Task Scheduler to run the normal command every 30–60 minutes:

1. Open **Task Scheduler → Create Basic Task**.
2. Set trigger to **Daily**, repeat every 30 minutes.
3. Set action to `python.exe` with argument `C:\path\to\scripts\outlook_sidecar.py`.

### What the sidecar sends

Each email item includes:

| Field | Notes |
|-------|-------|
| `item_id` | Outlook `EntryID` — used for deduplication |
| `title` | Subject line |
| `body` | Body text, truncated to 3000 characters, blank lines collapsed |
| `author` | `Name <email>` |
| `metadata.direction` | `received` or `sent` |
| `metadata.conversation_id` | Outlook `ConversationID` — used for graph threading |
| `metadata.conversation_topic` | Normalised subject (Re:/Fwd: stripped) |
| `metadata.is_read` | Whether the email has been read |
| `metadata.to` / `metadata.cc` | Recipient lists |
| `metadata.is_replied` | Whether you replied |

---

## 16. Thunderbird sidecar (Ubuntu)

### Install dependencies

```bash
pip install requests
```

### Configure Thunderbird

Thunderbird must store messages locally:

**Account Settings → Synchronization & Storage → Keep messages for this account on this computer**

### Run

```bash
python3 scripts/thunderbird_sidecar.py
```

### Schedule

Add to crontab to run every 30 minutes:

```bash
crontab -e
```

```
*/30 * * * * python3 /path/to/scripts/thunderbird_sidecar.py >> /tmp/parsival-sidecar.log 2>&1
```

---

## 17. Re-analysis

Re-analysis re-runs the LLM on all stored items using the current settings. Use it after:
- Adding new projects or keywords
- Changing your user name or email
- Updating the Ollama model
- Migrating from TinyDB to SQLite (backfills graph edges and new category schema)

### Trigger re-analysis

Click **Re-analyze** in the navigation, or:

```bash
curl -X POST http://localhost:8082/page/api/reanalyze
```

Poll progress via:

```bash
curl http://localhost:8082/page/api/scan/status
```

Re-analysis respects the same concurrency rules as a scan — you cannot start one if a scan or another re-analysis is already running (returns `409`).

### Count before running

Check how many items will be processed:

```bash
curl http://localhost:8082/page/api/reanalyze/count
```

---

## 18. Maintenance

### Wipe all data (keep settings)

```bash
curl -X POST http://localhost:8082/page/api/reset
```

Truncates `items`, `todos`, `intel`, `situations`, `scan_logs`, and `embeddings`. Settings are preserved.

### Wipe everything

```bash
docker compose down
rm data/parsival.db
docker compose up -d
```

### Migrate from TinyDB (one-time)

If you have a `data/page.db` from an older deployment:

```bash
docker compose down
sudo chown -R $(whoami):$(whoami) data/
python3 scripts/migrate_to_sqlite.py
docker compose up -d
```

Then run a re-analysis to backfill graph edges and the new category schema.

### View logs

```bash
docker compose logs page-api -f
```

### Interactive API docs

```
http://localhost:8082/page/api/docs
```

FastAPI's Swagger UI lists every endpoint with request/response schemas.

### Health check

```bash
curl http://localhost:8082/page/api/health
```

Returns `{"ok": true, "warnings": [...]}`. Warnings list any missing credentials or configuration issues.
