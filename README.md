# Hexcaliper Squire

A companion service for [Hexcaliper](https://github.com/rcanterberryhall/hexcaliper) that consolidates responsibilities from Outlook, Slack, GitHub, Jira, and Microsoft Teams into a single ops dashboard. Uses the Hexcaliper Ollama instance to extract action items, priority, goals, key dates, and context-aware relevance signals — no data leaves your infrastructure.

## Architecture

```
Browser (/page/)
  └── nginx (:8082)
        └── /page/api/* → FastAPI/uvicorn (:8001, service: page-api)
                            ├── Ollama (hexcaliper.com via Cloudflare Access)
                            ├── Slack API
                            ├── Microsoft Teams API (Graph)
                            ├── GitHub API
                            ├── Jira Cloud API
                            └── SQLite WAL  (./data/squire.db)

Email ingestion (host, not Docker):
  Windows  → scripts/outlook_sidecar.py      (win32com)
  Ubuntu   → scripts/thunderbird_sidecar.py  (local mbox/Maildir)
    └── POST /page/api/ingest → API
```

Runs alongside the existing Hexcaliper stack. Does not conflict with hexcaliper's ports (8080/8000).

| Port | Service                                    |
|------|--------------------------------------------|
| 8001 | FastAPI API (internal, bridge network)     |
| 8082 | nginx (web UI + API proxy, host-exposed)   |

| Component  | Technology                                                  |
|------------|-------------------------------------------------------------|
| Frontend   | Vanilla JS + CSS, served by nginx                           |
| API        | Python 3.12, FastAPI, uvicorn                               |
| Storage    | SQLite WAL (`squire.db`) with knowledge-graph tables        |
| LLM        | Ollama via Hexcaliper appliance (Cloudflare Access)         |
| Networking | Bridge (`app` network) — same pattern as Hexcaliper         |

## Connectors

| Source           | How                                | What it pulls                                       |
|------------------|------------------------------------|-----------------------------------------------------|
| Outlook          | Host sidecar script (win32com)     | Inbox and Sent emails with conversation threading   |
| Thunderbird      | Host sidecar script (mbox/Maildir) | Recent inbox emails with To/CC recipients           |
| Slack            | Per-user OAuth tokens              | @mentions, DMs, relevant channel messages           |
| Microsoft Teams  | Per-user OAuth tokens (Graph API)  | @mentions, DMs, relevant channel messages           |
| GitHub           | PAT REST API                       | Notifications, assigned issues, PR review requests  |
| Jira             | API token REST API                 | Open tickets assigned to current user               |

## Prerequisites

- Hexcaliper running with Ollama accessible at your configured endpoint
- A Cloudflare Access service token for the Ollama application
- Docker and Docker Compose (same versions as Hexcaliper)

## Setup

```bash
git clone <repo> hexcaliper-squire
cd hexcaliper-squire

# Edit docker-compose.yml and fill in your credentials
# (CTRL+F for "your-" to find all placeholders)

docker compose up --build -d

# Open in browser
xdg-open http://localhost:8082/page/
```

## Configuration

All credentials can be set in `docker-compose.yml` under the `page-api` environment block, or saved via the Settings page in the UI (which hot-reloads config without a container restart).

### Core settings

| Variable           | Description                                                                      |
|--------------------|----------------------------------------------------------------------------------|
| `CF_CLIENT_ID`     | Cloudflare Access service token ID                                               |
| `CF_CLIENT_SECRET` | Cloudflare Access service token secret                                           |
| `OLLAMA_URL`       | Ollama API endpoint (default: `https://ollama.hexcaliper.com/api/generate`)      |
| `OLLAMA_MODEL`     | Model for extraction (default: `llama3.2`)                                       |
| `LOOKBACK_HOURS`   | Hours of history per scan (default: `48`)                                        |

### User context

These fields are passed directly into every LLM prompt and are also used by the Slack and Teams connectors for pre-filtering.

| Variable       | Description                                                                                   |
|----------------|-----------------------------------------------------------------------------------------------|
| `USER_NAME`    | Your display name (e.g. `Jane Smith`)                                                         |
| `USER_EMAIL`   | Your email address (e.g. `jane.smith@company.com`)                                            |
| `FOCUS_TOPICS` | Comma-separated general watch-topic keywords (e.g. `kubernetes,cost reduction`)               |
| `PROJECTS`     | JSON array of project objects — see [Project configuration](#project-configuration) below     |

### Slack

| Variable              | Description                                                                          |
|-----------------------|--------------------------------------------------------------------------------------|
| `SLACK_CLIENT_ID`     | Slack app Client ID (for OAuth)                                                      |
| `SLACK_CLIENT_SECRET` | Slack app Client Secret                                                              |
| `SLACK_BOT_TOKEN`     | Legacy bot token (`xoxb-...`) — only used if no user tokens are connected            |
| `SLACK_CHANNELS`      | Comma-separated channel names for the legacy bot path. Empty = all joined channels   |

Connect your Slack workspaces via the Settings page (OAuth flow) to use per-user tokens instead of a bot token.

Required OAuth user scopes: `channels:history` `channels:read` `groups:history` `groups:read` `im:history` `im:read` `mpim:history` `mpim:read` `search:read` `users:read`

### Microsoft Teams

| Variable              | Description                                              |
|-----------------------|----------------------------------------------------------|
| `TEAMS_CLIENT_ID`     | Azure AD app Client ID (for OAuth)                       |
| `TEAMS_CLIENT_SECRET` | Azure AD app Client Secret                               |

Connect your Teams accounts via the Settings page (OAuth flow). Per-user tokens are stored at runtime; no static token env var is required. The OAuth flow uses the Microsoft identity platform and Microsoft Graph API.

### GitHub

| Variable          | Description                                        |
|-------------------|----------------------------------------------------|
| `GITHUB_PAT`      | GitHub PAT (scopes: `repo`, `notifications`)       |
| `GITHUB_USERNAME` | Your GitHub username                               |

### Jira

| Variable      | Description                                                                         |
|---------------|-------------------------------------------------------------------------------------|
| `JIRA_EMAIL`  | Jira account email                                                                  |
| `JIRA_TOKEN`  | Jira API token (https://id.atlassian.com/manage-profile/security/api-tokens)        |
| `JIRA_DOMAIN` | `yourco.atlassian.net`                                                              |
| `JIRA_JQL`    | JQL for your tickets (default: assignee = currentUser() AND statusCategory != Done) |

### Cloudflare Access service token

Zero Trust → **Access → Service Auth → Service Tokens** → Create token.
Copy Client ID and Client Secret (shown once). Add the token to the Access Policy protecting your Ollama application.

## Project configuration

Projects let Squire associate items with named workstreams. Each project is a JSON object with the following keys:

| Key                | Type             | Description                                                                                       |
|--------------------|------------------|---------------------------------------------------------------------------------------------------|
| `name`             | string           | Project name used for tagging and display                                                         |
| `description`      | string           | Optional free-text scope or purpose — passed to the LLM to disambiguate projects with similar names |
| `keywords`         | array of strings | Manually curated keywords — items matching these are tagged to this project                       |
| `channels`         | array of strings | Slack or Teams channel names monitored for this project                                           |
| `senders`          | array of strings | Manually curated email addresses or group aliases associated with this project                    |
| `parent`           | string           | Optional parent project name — when set and valid, the LLM prompt notes the sub-project relationship |
| `learned_keywords` | array of strings | Keywords learned via the tagging workflow (see [Project learning](#project-learning))             |
| `learned_senders`  | array of strings | Email addresses learned via tagging — senders/groups associated with this project                 |

Example `PROJECTS` value (set as an env var or saved via Settings):

```json
[
  {
    "name": "Platform Migration",
    "description": "Kubernetes migration from on-prem to EKS; owned by the platform team.",
    "keywords": ["k8s", "migration", "eks"],
    "channels": ["platform-eng", "infra-alerts"],
    "senders": ["platform-team@company.com"],
    "parent": "",
    "learned_keywords": [],
    "learned_senders": []
  }
]
```

LLM-returned `project_tag` values that do not match any configured project name are nulled out server-side before the analysis record is written, preventing spurious tags from polluting the project list.

## Context hierarchy

Every analysed item is assigned a `hierarchy` value indicating how directly it relates to you:

| Tier      | Meaning                                                                                         |
|-----------|-------------------------------------------------------------------------------------------------|
| `user`    | Directly addressed to you — your name/email in To/CC, a Slack DM or @mention, or an assignment  |
| `project` | Related to one of your active projects but not directly addressed to you                        |
| `topic`   | Matches a watch topic from `FOCUS_TOPICS` but not a specific project                            |
| `general` | Everything else                                                                                 |

The LLM assigns hierarchy based on prompt rules. The Slack and Teams connectors pre-compute hierarchy during channel pre-filtering and pass it as a hint in `item.metadata`.

## Category schema

Every analysed item is assigned a `category` and, for tasks, an optional `task_type`:

| Category    | Meaning                                                                         |
|-------------|---------------------------------------------------------------------------------|
| `task`      | Requires action from you. Sub-typed by `task_type` (`reply` or `review`)       |
| `approval`  | Needs your explicit approval or sign-off                                        |
| `fyi`       | Informational — no action required                                              |
| `noise`     | Irrelevant to you; suppressed from the main view                                |

`task_type` values:

| Value    | Meaning                                                                      |
|----------|------------------------------------------------------------------------------|
| `reply`  | The task is to compose and send a reply                                       |
| `review` | The task is to read/review a document, PR, or ticket                         |
| `null`   | General task not fitting either sub-type                                      |

## Project learning

When you tag an item to a project via `POST /analyses/{item_id}/tag`, Squire:

1. Immediately updates the stored analysis with the new `project_tag`.
2. Calls the LLM in the background to extract 5–10 characteristic keywords from the item's title and body preview.
3. Merges the new keywords (lowercased) into the project's `learned_keywords` list (capped at 100 entries).
4. Extracts all email addresses from the item's sender (`From`) and recipient (`To`/`CC`) fields.
5. Merges those addresses into the project's `learned_senders` list (capped at 50 entries), excluding your own address.
6. Saves the updated settings and hot-reloads config.

On the next scan, learned keywords are included in both the LLM prompt and the Slack/Teams pre-filter. Learned senders are checked deterministically before the LLM runs — if the incoming item's sender or any recipient address matches a `learned_senders` entry, the project tag is applied automatically. This covers both individual contacts who regularly email about a project and shared distribution lists that receive project-related traffic.

The manually configured `senders` array works the same way as `learned_senders` but is curated by hand rather than grown automatically.

## Noise filter

When you mark an item as noise via `POST /analyses/{item_id}/noise`, Squire:

1. Immediately sets `category="noise"`, `priority="low"`, and `has_action=false` on the stored analysis.
2. Calls the LLM in the background to extract keywords from the item.
3. Merges the keywords into `NOISE_KEYWORDS` (capped at 200 entries) and saves/reloads settings.

On subsequent Slack scans, messages that match only noise keywords (and no positive user/project/topic signal) are silently skipped. The LLM prompt also lists noise keywords so the model can set `category="noise"` for matching email and GitHub items.

## Passdown detection

Shift passdown / handoff notes are detected deterministically before the LLM runs. A passdown is identified when the subject line or the first 300 characters of the body contains any of the following patterns:

- The word **"passdown"**
- A phrase matching **"notes from \<word\> shift"** (e.g. "notes from 2nd shift", "notes from first shift")
- Any of the phrases: **"shift highlights"**, **"shift activities"**, **"shift notes"**, **"shift report"**, **"shift summary"**, **"shift handoff"**, **"shift update"**

When a pattern matches, `is_passdown` is forced to `true` regardless of the LLM response. Passdown items receive `has_action=false` by default unless the content explicitly directs an action at you by name.

## Email ingestion (sidecar scripts)

Email is fed into the API from the host machine — both Outlook (win32com) and Thunderbird (local mbox) require local client state not available inside Docker.  Both sidecars include `to` and `cc` fields in item metadata so the LLM can determine whether you are a direct recipient, a CC recipient, or absent from the header entirely.

CAUTION banners that mail clients prepend to external-sender messages are stripped from the stored `body_preview` so the LLM sees actual message content rather than boilerplate warnings.

### Windows — Outlook

```bash
pip install requests pywin32 keyring
python scripts/outlook_sidecar.py --setup   # first-time credential setup
python scripts/outlook_sidecar.py           # normal / scheduled run
```

Cloudflare Access credentials are stored in Windows Credential Manager via `keyring` and never written to disk. Run `--setup` once to store them, then schedule the normal run with Windows Task Scheduler every 30–60 minutes.

The sidecar fetches both **Inbox** and **Sent Items** and includes `conversation_id`, `conversation_topic`, and `direction` (`received`/`sent`) in each item's metadata. This allows the knowledge graph to link reply threads across sources and gives the LLM hierarchy context (sent items are treated as already-acted-upon).

Use `--seed` for first-time historical ingestion (last 30 days, up to 500 emails):

```bash
python scripts/outlook_sidecar.py --seed
```

### Ubuntu — Thunderbird

```bash
pip install requests
python scripts/thunderbird_sidecar.py
```

Thunderbird must keep messages locally:
**Account Settings → Synchronization & Storage → Keep messages for this account on this computer**

Add to crontab:
```
*/30 * * * * python3 /path/to/scripts/thunderbird_sidecar.py >> /tmp/page-sidecar.log 2>&1
```

Both sidecars POST to `/page/api/ingest`. The API deduplicates by message ID so re-running is safe.

## Seed workflow

The seed workflow bootstraps project intelligence from existing data when you first set up Squire, or after adding new projects. It walks through a guided state machine:

1. **Ingest first** — run your email sidecar (or any connector) to load existing items into the database.
2. **Start seed** (`POST /seed`) — the job enters `waiting_for_ingest` and polls until items arrive. An optional free-text `context` field in the request body is passed to the LLM to help identify projects. The context can be updated while waiting via `PATCH /seed/context`.
3. **Analysis** — the LLM reads a sample of stored items and proposes a list of projects and focus topics.
4. **Review** (`GET /seed/status`) — the frontend polls status and displays proposed projects/topics for the user to edit.
5. **Apply** (`POST /seed/apply`) — confirmed projects and topics are merged into settings. Existing analyses are re-tagged by keyword match. Embeddings are built and the situation layer is warmed.
6. **Re-analysis** — all stored items are re-run through the LLM with the new project list (`POST /reanalyze` equivalent).
7. **Optional scan** (`POST /seed/scan`) — run a live connector scan to pull fresh items. Or skip it with `POST /seed/skip_scan`.

Poll `GET /seed/status` throughout the flow. The returned state progresses through: `waiting_for_ingest` → `analyzing` → `review` → `reanalyzing` → `scan_prompt` → `scanning` → `done`.

## API endpoint reference

Interactive docs: `http://localhost:8001/docs`

### Health and settings

| Method   | Path        | Description                                                                                                                  |
|----------|-------------|------------------------------------------------------------------------------------------------------------------------------|
| `GET`    | `/health`   | Health check and config warnings                                                                                             |
| `GET`    | `/settings` | Current settings (secrets masked). Returns all config and credential fields                                                  |
| `POST`   | `/settings` | Persist and hot-reload settings. Fields containing `•` (masked) are ignored                                                  |

### Scanning

| Method | Path             | Description                                                                    |
|--------|------------------|--------------------------------------------------------------------------------|
| `POST` | `/scan`          | Start a scan (`{"sources": ["slack","github","jira","outlook","teams"]}`)      |
| `GET`  | `/scan/status`   | Poll scan progress and current item                                            |
| `POST` | `/scan/cancel`   | Cancel the current scan                                                        |
| `POST` | `/ingest`        | Receive raw items from sidecar scripts; deduplicates and queues AI analysis    |
| `POST` | `/reset`         | Truncate analyses, todos, and scan logs (settings are preserved)               |

### Re-analysis

| Method | Path               | Description                                                                    |
|--------|--------------------|--------------------------------------------------------------------------------|
| `GET`  | `/reanalyze/count` | Return the number of stored items that would be processed by a re-analysis run |
| `POST` | `/reanalyze`       | Re-run LLM on all stored items with current settings. Returns immediately; poll `GET /scan/status` for progress. Raises 409 if a scan or re-analysis is already running. |

### Analysis control

| Method | Path              | Description                                                                                           |
|--------|-------------------|-------------------------------------------------------------------------------------------------------|
| `POST` | `/analysis/stop`  | Gracefully halt all in-progress analysis — scan loop, reanalyze loop, ingest worker, situation formation, and seed state machine all exit after their current item finishes |

### Analyses

| Method  | Path                        | Description                                                                        |
|---------|-----------------------------|------------------------------------------------------------------------------------|
| `GET`   | `/analyses`                 | All analysed items, newest first (params: `source`, `category`). Returns up to 200 |
| `PATCH` | `/analyses/{item_id}`       | Update `priority`, `category`, `task_type`, `project_tag`, or `is_passdown`. Setting `category="noise"` also clears `has_action` and removes associated todos; changing `priority` syncs to associated todo rows. Categories: `task`, `approval`, `fyi`, `noise` |
| `POST`  | `/analyses/{item_id}/tag`   | Tag item to a project; triggers background keyword/sender learning                 |
| `POST`  | `/analyses/{item_id}/noise` | Mark item as irrelevant; triggers background noise keyword learning                |

### Situations

Situations are cross-source groupings of related analyses identified automatically by the correlation layer. Each situation has a composite urgency score, a status, and a list of open actions.

| Method   | Path                                  | Description                                                                        |
|----------|---------------------------------------|------------------------------------------------------------------------------------|
| `GET`    | `/situations`                         | List non-dismissed situations (params: `project`, `status`, `min_score`), sorted by score descending |
| `GET`    | `/situations/{id}`                    | Return a single situation with full deserialized analyses in the `items` field     |
| `POST`   | `/situations/{id}/dismiss`            | Mark situation as dismissed. Optional `{"reason": "..."}` body stores a dismiss reason |
| `POST`   | `/situations/{id}/rescore`            | Manually trigger score recomputation and LLM re-synthesis for a situation          |
| `PATCH`  | `/situations/{id}`                    | Update `title`, `status`, or `project_tag` on a situation                          |

### Intel

Intel items are key facts and completed-action notes extracted by the LLM that are worth knowing but are not action items for the user.

| Method   | Path              | Description                                                                       |
|----------|-------------------|-----------------------------------------------------------------------------------|
| `GET`    | `/intel`          | List non-dismissed intel items (params: `source`, `project`), newest first        |
| `DELETE` | `/intel/{id}`     | Permanently delete an intel item                                                  |
| `PATCH`  | `/intel/{id}`     | Update an intel item — currently supports toggling the `dismissed` flag            |

### Projects

| Method | Path        | Description                                                                          |
|--------|-------------|--------------------------------------------------------------------------------------|
| `GET`  | `/projects` | List configured projects with manual keywords, channels, and learned keyword counts  |

### Todos

| Method   | Path          | Description                                                                                             |
|----------|---------------|---------------------------------------------------------------------------------------------------------|
| `GET`    | `/todos`      | List action items (`source`, `priority`, `done`). Sorted by priority                                    |
| `POST`   | `/todos`      | Create a manual action item (`{"description": "...", "priority": "high", "deadline": "2026-12-31", "project_tag": "..."}`) |
| `PATCH`  | `/todos/{id}` | Update a todo — `done`, `description`, `deadline`, `priority`, `project_tag`                           |
| `DELETE` | `/todos/{id}` | Delete a todo                                                                                           |

### Stats

| Method | Path     | Description                                                                 |
|--------|----------|-----------------------------------------------------------------------------|
| `GET`  | `/stats` | Aggregate counts, open todos by source, items by category, last scan info   |

### Slack OAuth

| Method   | Path                          | Description                                                        |
|----------|-------------------------------|--------------------------------------------------------------------|
| `GET`    | `/slack/connect`              | Redirect to Slack OAuth authorisation page                         |
| `GET`    | `/slack/callback`             | OAuth callback — exchanges code for user token, saves to settings  |
| `GET`    | `/slack/workspaces`           | List connected workspaces (team name and ID)                       |
| `DELETE` | `/slack/workspaces/{team_id}` | Disconnect a workspace                                             |

### Microsoft Teams OAuth

| Method   | Path                             | Description                                                              |
|----------|----------------------------------|--------------------------------------------------------------------------|
| `GET`    | `/teams/connect`                 | Redirect to Microsoft identity platform OAuth authorisation page         |
| `GET`    | `/teams/callback`                | OAuth callback — exchanges code for user token, saves to settings        |
| `GET`    | `/teams/workspaces`              | List connected accounts (display name, account ID, tenant)               |
| `DELETE` | `/teams/workspaces/{account_id}` | Disconnect a Teams account                                               |

### Seed workflow

| Method  | Path              | Description                                                                                                    |
|---------|-------------------|----------------------------------------------------------------------------------------------------------------|
| `POST`  | `/seed`           | Start the seed state machine. Optional `{"context": "..."}` body. Returns immediately with current job state   |
| `PATCH` | `/seed/context`   | Update the context string while the job is in `waiting_for_ingest` state                                       |
| `GET`   | `/seed/status`    | Poll the current seed job state (includes proposed `projects` and `topics` when state is `review`)             |
| `POST`  | `/seed/apply`     | Apply confirmed projects and topics to settings; triggers re-tagging, embedding, and re-analysis               |
| `POST`  | `/seed/scan`      | Advance from `scan_prompt` to `scanning` — runs a full connector scan, then transitions to `done`              |
| `POST`  | `/seed/skip_scan` | Advance from `scan_prompt` to `done` without running a connector scan                                          |

## Data persistence

Stored in `./data/squire.db` (SQLite WAL). Bind-mounted and survives restarts.

```bash
docker compose down
rm data/squire.db   # wipe all data
docker compose up -d
```

Alternatively, use `POST /reset` to clear analyses, todos, and scan logs while keeping your saved settings.

### Migrating from an older TinyDB installation

If you have an existing `data/page.db` from a TinyDB-based deployment, run the one-time migration script before starting the new container:

```bash
python scripts/migrate_to_sqlite.py
```

## Knowledge graph

Every analysed item is indexed into a lightweight knowledge graph (`api/graph.py`) stored in the same SQLite database. Node types are `item`, `person`, `project`, and `conversation`. Edges carry typed relationships:

| Edge type       | Weight | Meaning                                          |
|-----------------|--------|--------------------------------------------------|
| `in_conversation` | 1.00 | Two emails share the same Outlook ConversationID |
| `in_situation`    | 0.80 | Two items grouped into the same situation        |
| `tagged_to`       | 0.55 | Item tagged to a project                         |
| `authored_by`     | 0.40 | Item sent or created by the same person          |

When the LLM analyses an item, up to four related items are retrieved from the graph, scored by edge weight × recency decay (14-day half-life), and injected into the prompt as `GraphRAG context`. This lets the model reason about conversation threads and project workstreams across sources without re-scanning all history.

## Phase 4c features

### Scan progress estimation

`GET /scan/status` now returns three additional fields alongside the existing response:

| Field                          | Type    | Description                                                                                    |
|--------------------------------|---------|------------------------------------------------------------------------------------------------|
| `total_items`                  | integer | Total items to be processed in the current run                                                 |
| `completed_items`              | integer | Items processed so far                                                                         |
| `estimated_minutes_remaining`  | float   | Rolling estimate based on average per-item time across the last 10 items; 0 when not running  |

The progress bar in the UI is driven by `completed_items / total_items * 100` when these fields are present, and the status message shows `Analyzing N/Total items — ~X min remaining` while analysis is in progress.

### Re-analysis item ordering

`POST /reanalyze` now processes items in priority order rather than insertion order:

1. `user` tier first (items directly addressed to you)
2. `project` tier second
3. `topic` tier third
4. `general` tier last

Within each tier, items are processed newest-first by timestamp, ensuring your most time-sensitive context is refreshed earliest in every re-analysis run.

### Night-mode batch processing (merLLM integration)

When `POST /reanalyze` is triggered, Parsival checks `GET {MERLLM_URL}/api/merllm/status`. If the response is `{"mode": "night", ...}`, re-analysis jobs are submitted to `POST {MERLLM_URL}/api/batch/submit` instead of calling Ollama directly. This avoids contending with interactive GPU use during off-hours.

Each submitted job stores a `batch_job_id` on the item record. A background polling thread (60-second interval) checks `GET {MERLLM_URL}/api/batch/result/{job_id}` for each pending job, parses the completed response, and applies the result exactly as a direct analysis would — updating the analysis record, todos, intel, knowledge graph, and situation formation.

If merLLM is unreachable or returns a non-night mode, re-analysis proceeds with direct Ollama calls as normal. If a batch submission fails for an individual item, that item falls back to direct Ollama automatically.

Set the `MERLLM_URL` environment variable (default: `http://host.docker.internal:11400`) to point at your merLLM instance.
