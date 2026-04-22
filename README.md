# Parsival

Parsival is one of three apps in the [Hexcaliper](https://github.com/rcanterberryhall) ecosystem — a sibling of [LanceLLMot](https://github.com/rcanterberryhall/hexcaliper-lanceLLMot) (document assistant) and [merLLM](https://github.com/rcanterberryhall/hexcaliper-merLLM) (GPU scheduler). It consolidates responsibilities from Outlook, Slack, GitHub, Jira, and Microsoft Teams into a single ops dashboard. Uses the shared Ollama instance (via merLLM) to extract action items, priority, goals, key dates, and context-aware relevance signals — no data leaves your infrastructure.

Parsival includes scheduled auto-scans, an adaptive attention model that learns from your behavior, a situation lifecycle workflow, pre-scan noise filters, and encrypted credential storage.

## Architecture

```
Browser (/page/)
  └── nginx (:8082)
        └── /page/api/* → FastAPI/uvicorn (:8001, service: page-api)
                            ├── llm.py → Ollama local / Ollama Cloud / Claude API
                            ├── Slack API
                            ├── Microsoft Teams API (Graph)
                            ├── GitHub API
                            ├── Jira Cloud API
                            └── SQLite WAL  (./data/parsival.db)

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
| Storage    | SQLite WAL (`parsival.db`) with knowledge-graph tables        |
| LLM        | Ollama (local or cloud) or Claude API, via `llm.py` provider abstraction |
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
git clone <repo> hexcaliper-parsival
cd hexcaliper-parsival

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
| `OLLAMA_URL`       | Ollama API endpoint (default: `http://host.docker.internal:11400/api/generate`)  |
| `OLLAMA_MODEL`     | Model for extraction (default: `qwen3:32b`)                                     |
| `MERLLM_URL`       | merLLM base URL for batch jobs (default: `http://host.docker.internal:11400`)    |
| `LOOKBACK_HOURS`   | Hours of history per scan (default: `48`)                                        |
| `CREDENTIALS_KEY`  | Passphrase for Fernet encryption of OAuth tokens at rest. Leave unset for plaintext (backward compatible). Changing this key after tokens are stored makes them unreadable. |

### Analysis provider (escalation model)

All LLM calls (analysis, seeding, correlation, briefing) route through the `llm.py` provider abstraction. Three backends are supported:

| Variable              | Description                                                                      |
|-----------------------|----------------------------------------------------------------------------------|
| `ESCALATION_PROVIDER` | `ollama` (default), `ollama_cloud`, or `claude`                                  |
| `ESCALATION_MODEL`    | Model override — when set, used instead of `OLLAMA_MODEL` (e.g. `llama3:70b`, `claude-sonnet-4-20250514`) |
| `ESCALATION_API_KEY`  | API key for Ollama Cloud (Bearer token) or Claude (`x-api-key`). Not needed for local Ollama. |
| `ESCALATION_API_URL`  | API base URL for Ollama Cloud (e.g. `https://api.ollama.com`). Claude uses the default `https://api.anthropic.com`. |

**Provider details:**

- **`ollama` (local):** Calls `OLLAMA_URL` directly. Uses `ESCALATION_MODEL` if set, otherwise `OLLAMA_MODEL`. No API key needed.
- **`ollama_cloud`:** Calls `ESCALATION_API_URL` with a Bearer token from `ESCALATION_API_KEY`. Same Ollama API format as local but routed to the paid cloud endpoint.
- **`claude`:** Calls the Anthropic Messages API (`https://api.anthropic.com/v1/messages`). Requires `ESCALATION_API_KEY`. Defaults to `claude-sonnet-4-20250514` if `ESCALATION_MODEL` is not set. JSON mode is enforced via a system prompt.

All provider settings are configurable from the Settings UI under "Analysis Provider" and hot-reload without a container restart.

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
| `SLACK_REDIRECT_URI`  | OAuth callback URL (default: `https://parsival.hexcaliper.com/page/api/slack/callback`) |
| `SLACK_BOT_TOKEN`     | Legacy bot token (`xoxb-...`) — only used if no user tokens are connected            |
| `SLACK_CHANNELS`      | Comma-separated channel names for the legacy bot path. Empty = all joined channels   |

Connect your Slack workspaces via the Settings page (OAuth flow) to use per-user tokens instead of a bot token.

Required OAuth user scopes: `channels:history` `channels:read` `groups:history` `groups:read` `im:history` `im:read` `mpim:history` `mpim:read` `search:read` `users:read`

Every message the connector surfaces is recorded in the `slack_seen_messages` table, so subsequent scans skip timestamps it has already ingested. Channel and DM aggregate items use a stable `(team, channel)` id and are only emitted when there's genuinely new content — todos you've already acted on are not re-created the next time the scheduler fires.

### Microsoft Teams

| Variable              | Description                                              |
|-----------------------|----------------------------------------------------------|
| `TEAMS_CLIENT_ID`     | Azure AD app Client ID (for OAuth)                       |
| `TEAMS_CLIENT_SECRET` | Azure AD app Client Secret                               |
| `TEAMS_REDIRECT_URI`  | OAuth callback URL (default: `https://parsival.hexcaliper.com/page/api/teams/callback`) |

Connect your Teams accounts via the Settings page (OAuth flow). Per-user tokens are stored at runtime; no static token env var is required. The OAuth flow uses the Microsoft identity platform and Microsoft Graph API.

### GitHub

| Variable          | Description                                        |
|-------------------|----------------------------------------------------|
| `GITHUB_PAT`      | GitHub PAT (scopes: `repo`, `notifications`)       |
| `GITHUB_USERNAME` | Your GitHub username                               |
| `GITHUB_MAX_NOTIFICATIONS` | Max notifications to fetch across paginated results (default: `500`) |

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

Projects let Parsival associate items with named workstreams. Each project is a JSON object with the following keys:

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

When you tag an item to a project via `POST /analyses/{item_id}/tag`, Parsival:

1. Immediately updates the stored analysis with the new `project_tag`.
2. Calls the LLM in the background to extract 5–10 characteristic keywords from the item's title and body preview.
3. Merges the new keywords (lowercased) into the project's `learned_keywords` list (capped at 100 entries).
4. Extracts all email addresses from the item's sender (`From`) and recipient (`To`/`CC`) fields.
5. Merges those addresses into the project's `learned_senders` list (capped at 50 entries), excluding your own address.
6. Saves the updated settings and hot-reloads config.

On the next scan, learned keywords are included in both the LLM prompt and the Slack/Teams pre-filter. Learned senders are checked deterministically before the LLM runs — if the incoming item's sender or any recipient address matches a `learned_senders` entry, the project tag is applied automatically. This covers both individual contacts who regularly email about a project and shared distribution lists that receive project-related traffic.

The manually configured `senders` array works the same way as `learned_senders` but is curated by hand rather than grown automatically.

## Noise filter

When you mark an item as noise via `POST /analyses/{item_id}/noise`, Parsival:

1. Immediately sets `category="noise"`, `priority="low"`, and `has_action=false` on the stored analysis.
2. Calls the LLM in the background to extract keywords from the item.
3. Merges the keywords into `NOISE_KEYWORDS` (capped at 200 entries) and saves/reloads settings.

On subsequent Slack scans, messages that match only noise keywords (and no positive user/project/topic signal) are silently skipped. The LLM prompt also lists noise keywords so the model can set `category="noise"` for matching email and GitHub items.

## Pre-scan noise filters

Noise filters are evaluated *before* the LLM runs, allowing known-irrelevant items to be skipped without spending an inference cycle. Filtered items are stored with status `filtered` (auditable, not discarded) and skip the LLM pipeline entirely.

Rule types:
- `sender_contains` — skip items from matching senders (e.g. `"noreply@"`)
- `subject_contains` — skip by subject/title substring (e.g. `"Out of Office"`)
- `source_repo` — skip GitHub notifications from specific repos
- `distribution_list` — skip emails to large distribution lists

Configure filters in Settings → Filters. The UI shows the count of items filtered in the last scan. When marking an item as noise in the main view, a "Create filter from this?" option pre-fills a rule based on the item's sender/source/subject.

Manage filters via the API:
- `GET /noise-filters` — list all rules
- `POST /noise-filters` — add a rule
- `DELETE /noise-filters/{index}` — remove a rule

## Scheduled auto-scans

Parsival can scan each connector on a schedule, eliminating the need to remember to trigger scans manually.

Configure via Settings → Schedule: each connector has an interval dropdown (off / 15m / 30m / 1h / 2h / 4h). The schedule is stored as `scan_schedule` in settings — a dict of `{source: interval_minutes}` where 0 = manual only.

Auto-scans run the same `orchestrator.run_scan()` path as manual scans but only for the scheduled source. Overlapping scans are skipped (if a scan is already running, the scheduled one is logged and deferred).

Check schedule status via `GET /scan/status`, which now includes:
```json
{
  "auto_scans": {
    "slack": {"next_run": "...", "last_run": "...", "interval_min": 30},
    "github": {"next_run": "...", "last_run": "...", "interval_min": 60}
  }
}
```

## Situation lifecycle workflow

Situations now have a formal status workflow instead of only being dismissable:

| Status | Meaning |
|---|---|
| `new` | Freshly identified by the correlation layer |
| `investigating` | You are actively looking into it |
| `waiting` | Blocked or pending external input; has an optional `follow_up_date` |
| `resolved` | Completed — no further action needed |
| `dismissed` | Irrelevant (existing behavior, formalized) |

Each situation card has a status dropdown and a "follow up" date picker. Status transitions are logged in a `situation_events` table (situation_id, from_status, to_status, timestamp, note).

The situations list defaults to showing `new` + `investigating` + `waiting` statuses. Resolved/dismissed are accessible via a toggle. Situations in `waiting` status past their `follow_up_date` surface at the top with a visual indicator.

**Stale-decay flag.** Situations in `waiting` for ≥ 7 days or in `investigating` for ≥ 14 days with no newer `situation_events` entry get a `stale_flag` of `stale_waiting` or `stale_investigating` respectively. The flag is advisory — nothing auto-closes; the UI renders an amber badge so the user can decide to transition the lifecycle.

**Manual split / merge.** The correlator occasionally groups items that should be separate threads, or splits threads that belong together. Two endpoints fix that by hand:
- `POST /situations/{id}/split` — move a subset of items into a brand-new situation. The source must retain at least one item; both situations are lightweight-rescored (no LLM).
- `POST /situations/{id}/merge` — absorb another situation's items into this one. The source is dismissed with `dismiss_reason="merged_into:<target>"` so history is preserved.

After a split or merge, run `POST /situations/{id}/rescore` to regenerate titles and summaries via the LLM.

API additions:
- `PATCH /situations/{id}` accepts `title`, `status`, `lifecycle_status`, `follow_up_date`, `notes`, `project_tag`
- `GET /situations/{id}/events` — returns the status transition history
- `POST /situations/{id}/split` — split a situation by item IDs
- `POST /situations/{id}/merge` — merge another situation into this one
- `POST /situations/{id}/transition` — explicit lifecycle transition with optional note and follow-up date

## Adaptive attention model

Replaces the fixed 4-tier priority hierarchy with a learned attention score based on your actual behavior. No configuration needed — it watches what you do and adapts.

**Tracked signals** (stored in `user_actions` table):
- `opened` — clicked to view full analysis
- `tagged` — assigned a project tag
- `noised` — marked as noise
- `dismissed_situation` / `investigated_situation` — situation status changes
- `deep_analysis` — submitted for deep analysis
- `todo_created` — created a todo from an item

**How it works:**
- Two centroid embeddings are maintained incrementally: `attended_centroid` (items you opened, tagged, investigated) and `ignored_centroid` (items untouched for 48h, or explicitly noised).
- For each new item: `attention_score = cosine_sim(item, attended) - 0.5 * cosine_sim(item, ignored)`, normalized to 0–1.
- Recency decay: actions older than 30 days contribute at 50% weight, older than 60 days at 25%.
- Centroids are stored in a `model_state` table and updated incrementally (no full recomputation).

**UI integration:**
- Items are sorted by attention score by default (switchable to chronological or LLM-priority).
- High-attention items have a subtle visual weight (bolder border).
- Briefing generation weights items by attention score.

**Cold start:** until 50 user actions are recorded, the system falls back to the existing LLM priority tier and displays "Learning your attention patterns — prioritization will improve as you use the tool."

API addition:
- `GET /attention/summary` — returns attention model summary for the merLLM "My Day" panel (situation counts, follow-up counts, high-attention items)

## OAuth security

OAuth flows for Slack and Teams include CSRF protection:
- A random `state` nonce is generated on each `/slack/connect` and `/teams/connect` request and stored with a 10-minute TTL.
- The callback validates the `state` parameter and returns 403 if missing or mismatched.
- Expired state tokens are cleaned up periodically.

Redirect URIs are configurable via `SLACK_REDIRECT_URI` and `TEAMS_REDIRECT_URI` env vars.

## GitHub pagination

The GitHub connector follows `Link: rel="next"` headers and pages through all results up to `GITHUB_MAX_NOTIFICATIONS` (default 500). This prevents silently missing notifications for heavy GitHub users.

## Passdown detection

Shift passdown / handoff notes are detected deterministically before the LLM runs. A passdown is identified when the subject line or the first 300 characters of the body contains any of the following patterns:

- The word **"passdown"**
- A phrase matching **"notes from \<word\> shift"** (e.g. "notes from 2nd shift", "notes from first shift")
- Any of the phrases: **"shift highlights"**, **"shift activities"**, **"shift notes"**, **"shift report"**, **"shift summary"**, **"shift handoff"**, **"shift update"**

When a pattern matches, `is_passdown` is forced to `true` regardless of the LLM response. Passdown items receive `has_action=false` by default unless the content explicitly directs an action at you by name.

## Passdown generator

Parsival can assemble a shift-handoff email from recent activity. The 📋 Passdown button above the action list opens a modal with a live HTML preview and editable textarea. Five sections are emitted:

1. **Open action items** — todos still open (highest priority first).
2. **Active situations** — non-dismissed situations with their lifecycle status and open actions.
3. **Upcoming deadlines** — next 12 hours of `due_at` todos and `follow_up_date` situations.
4. **Recent high-priority items** — items flagged `high` in the look-back window.
5. **Recently replied** — items with a `replied_at` in the window (a proxy for "closed loop today").

The lookback window is user-selectable (6 h / 12 h / 24 h / 48 h / 1 week). Two copy buttons are exposed: **Copy HTML** (uses `ClipboardItem` with both `text/html` and `text/plain` payloads so Outlook preserves formatting) and **Copy text** (plain-text fallback). Nothing is sent automatically — the user owns the final message.

API addition:
- `POST /passdown/generate` — body: `{"hours": <int>}` (clamped to `1..168`). Returns `{"html": "...", "sections": {...}, "generated_at": "ISO", "hours": <int>}`.

## Priority override feedback

When the user changes an item's priority from the detail panel, the UI prompts for a one-click reason:

- `person_matters` — this sender/author matters for me
- `topic_hot` — this topic is hot right now
- `deadline_real` — the deadline is actually binding
- `other` — freeform / none of the above

If the user picks a reason, the override (LLM-assigned priority, user-corrected priority, reason, item title snippet, timestamp) is appended to `settings.priority_overrides` (capped at the last 100 entries) and applied to `config.PRIORITY_OVERRIDES`. Future analysis prompts include a summary of recent overrides grouped by reason so the LLM can weight similar items correctly. Picking **Skip** changes the priority without recording a reason.

The mechanism intentionally mirrors the existing `ASSIGNMENT_CORRECTIONS` pattern — feedback is prompt-injected at inference time rather than used to retrain anything.

## Mobile layout

Narrow viewports get progressive adaptations instead of a separate mobile build:

| Breakpoint | Behavior |
|---|---|
| ≤ 916 px | Foldable-inner width. A **Right Now** summary card appears at the top of the main column showing the top 3 attention-ranked items, overdue todo count, next situation follow-up date, and a one-tap Scan button. Items table hides the Summary / Link columns. |
| ≤ 768 px | Sidebar becomes an overlay drawer (☰ hamburger toggles). Items view switches from a table to a **single-column card layout** — each card shows source, category, title, priority, and author. Detail panel and settings modal go full-screen. Interactive controls grow to ≥ 34 px tap targets. Situation-card actions wrap below the title row. **Swipe gestures** on todo rows: swipe right to mark done, swipe left to delete. |
| ≤ 480 px | Topbar wordmark subtitle is hidden. |
| ≤ 430 px | Foldable-folded outer display. `Seed` and `Re-analyze` are hidden from the topbar — run them from the unfolded view or a desktop session. Wordmark and situation typography tighten. |

`web/page/` is nginx-volume-mounted, so CSS changes are picked up without rebuilding the image.

### Right Now panel

The Right Now card is driven entirely by data already loaded into the page — no new API endpoint. It pulls the top 3 rows from `allAnalyses` sorted by `attention_score` descending (noise / replied items excluded), counts overdue deadlines from `allTodos`, and reads the nearest `follow_up_date` from `/situations`. It refreshes automatically when `loadItems` / `loadTodos` completes, and the in-panel Scan button re-uses the topbar's `startScan()` flow.

### Swipe gestures

Swipe handlers are attached at the document level behind a `window.innerWidth ≤ 768` gate, so desktop pointer events are unaffected. Thresholds: 80 px horizontal travel triggers the action; smaller swipes snap back. Interactive elements inside the todo row (buttons, links, checkboxes, chips) short-circuit the gesture so taps still work.

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

Use `--seed-and-infer` to ingest history **and** automatically start LLM project inference. This calls `POST /seed` after ingest completes, runs the map-reduce analysis, and polls until the LLM has proposed projects — then prints the UI URL for you to review and confirm them. Re-analysis of all items runs automatically after you apply.

```bash
python scripts/outlook_sidecar.py --seed-and-infer
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

## Cards

Todos appear as **cards** in the right-hand detail panel. Every card — manual or LLM-generated — exposes the same editable surface: title, summary, body/notes, priority, category, project tag, goals, key dates, linked tasks, and "why this priority". Edits on generated cards are preserved across reanalysis via `user_edited_fields`.

Click **+ New card** in the Todos toolbar to create a manual card. A blank card is inserted (`source='manual'`, `item_id='manual_<doc_id>'`) and the detail panel opens straight into editing. Manual cards are skipped by the reanalyze orchestrator, so the LLM never touches them.

## Look-ahead board

A two-week planning view that sits alongside todos, situations, and intel. Each project gets its own 14-day board anchored to a Sun–Sat calendar week — the window always covers two full calendar weeks and can be paged back/forward in 2-week increments via the `◀ Today ▶` toolbar controls. A global overview rolls up every project sorted by the earliest card start. Cards are UUID-keyed, carry a status (`planned`, `in_progress`, `done`, `blocked`), optional assignee, start/end date + shift, and three kinds of relations:

A color legend below the toolbar explains the visual encoding: card status is shown as a colored left border (gray = planned, blue = in progress, green = done, red = blocked), today's column has an amber tint, overdue cards sit in a red-bordered gutter, and missing-resource chips use a red highlight.

- **Dependencies** — other look-ahead cards that must finish first
- **Links** — cross-system references to `todo`, `situation`, or `key_date` targets
- **Resources (BOM)** — entries from the global resource catalog with a per-card status of `needed` / `secured` / `consumed`

Resources are typed (`person`, `equipment`, `space`, `part`, `supply`) and shared across all projects. Each project owns its own shift schedule (up to 6 shifts per day with human-readable `HH:MM` start/end and a comma-separated day-of-week mask like `M,T,W,Th,F`). A card's `start_shift_num` and `end_shift_num` delimit the inclusive range of shifts the task occupies, so a single card can span multiple shifts (e.g. shift 1 + 2 on the same day) or stay confined to a single shift across multiple days (e.g. 2nd-shift-only for three days); multi-shift cards render a blue range chip on the board to make the span visible at a glance.

Open the board from the vertical tab rail. Use the toolbar to switch between overview and a specific project, filter by assignee / status / resource (including a "missing resources" preset that surfaces cards with unsecured BOM items), drag a card between days to reschedule, or click a card to edit its BOM and cross-system links. The "Resources" and "⚙ Shifts" buttons open catalog / schedule editors. Phase one is local-only; templates (#49) and LLM-driven cross-system linking (#50) land in follow-up issues.

In the card editor, the assignee input autocompletes from both prior card assignees and the contacts table (`/contacts`) — typing "Jo" surfaces "Jose" even if no card has been assigned to them yet. BOM status badges in the card modal are click-to-cycle (`needed` → `secured` → `consumed`) and persist immediately via `PATCH /lookahead/cards/{card_id}/resources/{resource_id}`.

Each card (and each template task) carries an optional **`work_days`** mask — a comma-separated DOW list in the same `M,T,W,Th,F,Sa,Su` format as `project_shifts.days` — so the workweek is per-task, not per-template. The card editor and template editor expose preset options (7-day / Mon-Fri / Mon-Thu / Sun-Wed / Custom); an empty mask means "no restriction" and falls back to the template's `duration_unit`. When the mask is set and covers fewer than 7 days, date math (instantiation offsets and duration-to-end-date) counts only those days and reschedule shifts the card by its own work-day count within the (old, new) anchor window. Cards render a diagonal-hatch overlay on DOW cells outside the mask so spans across "off" days read as skipped on the board.

The board is mobile-friendly — on narrow screens the 14-day grid collapses to a vertical day-list so the view works on foldables.

Every card is mirrored by a linked action item (todo). Creating a card inserts a matching manual todo (`source=lookahead`, deadline = card end date, owner = card assignee, project_tag = card project); deleting the card deletes the todo. Marking either side done flips the other: closing the todo sets the card to `done`, closing the card sets the todo to `done`, and re-opening the todo flips the card back to `planned`. On first start after upgrade the server back-fills todos for every pre-existing card (once per database, guarded by a `model_state` marker).

### Templates

Repeatable work can be saved as a **template**: a small graph of tasks with relative offsets, optional dependencies, and resource requirements. Templates live in the "Templates" button on the look-ahead toolbar.

- **Authoring** — each template has a name, owner, optional default project, and a `duration_unit` of `calendar_days` or `business_days`. Tasks carry a `local_id`, an offset from the instance start date, a starting shift (1/2/3), a duration in shifts, a comma-separated list of dependencies (other `local_id`s in the same template), an optional per-task `work_days` mask (overrides the template's unit for that task only — Mon-Thu crews and Sun-Wed shifts coexist with Mon-Fri tasks in the same template), and optional named-resource requirements. Editing a template bumps its `version`; existing instances keep their snapshot.
- **Instantiation** — pick a template, pass a `start_date` and `project_tag`, and the backend materialises one card per task with absolute dates computed from the template's unit. Template-local dependencies translate into concrete card dependencies. Named resource requirements become BOM entries with `status=needed`; generic role-only requirements are left for manual assignment on the card.
- **Reschedule** — PATCH an instance with a new `start_date` and every attached card shifts by the same delta (business-day templates stay aligned to weekdays).
- **Detach** — `POST /lookahead/cards/{id}/detach` pops a card out of its instance so later reschedules leave it alone. The card itself stays.
- **Auto-complete** — when every attached card reaches `status=done`, the instance flips to `complete` automatically.
- **Opt-in upgrade** — after a template is edited, its existing instances report `outdated: true` alongside the new `template_current_version`. `POST /lookahead/instances/{id}/upgrade` re-applies the latest template: refreshes title / schedule / procedure-doc on existing cards, creates cards for newly-added tasks, leaves orphaned cards attached (their task was removed), rebuilds dependencies, and adds any newly-required named resources without touching user-edited BOM rows. Assignee, status, and notes are always preserved. The instances panel surfaces the version gap and an "Upgrade" button per instance so the user decides per cohort.

Nested templates stay out of scope for v2; cross-system LLM linking lands with #50.

### Cross-system linking

Cards can reach into the rest of Parsival's information stream:

- **Procedure docs.** Any card — manual or template-spawned — can carry a `linked_procedure_doc` URL. The card editor renders a "View procedure →" button next to the field, and cards with a procedure doc get a small `📄` chip on the board. Template tasks may declare the URL once so every instantiated card inherits it.
- **Item links.** The card editor accepts `item` as a link type, pointing at an item_id from the analyses table (emails / Slack / Teams / Jira messages Parsival has seen). Cards with any external link show a `🔗 N` counter on the board.
- **LLM-suggested connections.** Click "Find suggestions" inside a card's editor to ask the LLM for items in the same project that look related. Each proposal lists title, source, and a one-line reason; accept turns it into a concrete link, reject drops it. The toolbar "✨ Annotate" button fans the annotator out across every card in the current 14-day window. User confirmation is always required — no auto-linking.
- **Filters.** The toolbar gained a "with external links" / "no linked context" filter so you can either focus on cards that already have their paperwork attached or hunt for the ones still waiting to be annotated.

Jira sprint bridging (surfacing Jira issues on the board directly) is on hold until a Jira ingest path lands — until then, accepted item-links are the entry point for Jira-sourced context.

## Seed workflow

The seed workflow bootstraps project intelligence from existing data when you first set up Parsival, or after adding new projects. It walks through a guided state machine:

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
| `PATCH` | `/analyses/{item_id}`       | Update `priority`, `category`, `task_type`, `project_tag`, or `is_passdown`. Setting `category="noise"` also clears `has_action` and removes associated todos; changing `priority` syncs to associated todo rows. Categories: `task`, `approval`, `fyi`, `noise`. When `priority` changes, an optional `priority_reason` in `{person_matters, topic_hot, deadline_real, other}` records a priority-override entry that's injected into future analysis prompts (see [Priority override feedback](#priority-override-feedback)). |
| `POST`  | `/analyses/{item_id}/tag`   | Tag item to a project; triggers background keyword/sender learning                 |
| `POST`  | `/analyses/{item_id}/noise` | Mark item as irrelevant; triggers background noise keyword learning                |

### Situations

Situations are cross-source groupings of related analyses identified automatically by the correlation layer. Each situation has a composite urgency score, a lifecycle status, optional follow-up date, and a list of open actions.

The narrative synthesizer is aware of completed work: when a situation is formed or re-synthesized, done todos attached to the cluster's items are fed into the prompt under a "Completed actions (treat as already done; do not re-raise)" block, so the `summary` reflects the current state instead of restating finished work as still pending, and `open_actions` no longer surfaces items the user has already closed.

| Method   | Path                                  | Description                                                                        |
|----------|---------------------------------------|------------------------------------------------------------------------------------|
| `GET`    | `/situations`                         | List situations (params: `project`, `status`, `min_score`). Defaults to `new` + `investigating` + `waiting` statuses, sorted by score descending. Overdue follow-ups surface at the top. Each response includes a `stale_flag` of `stale_waiting`, `stale_investigating`, or `null`. |
| `GET`    | `/situations/{id}`                    | Return a single situation with full deserialized analyses in the `items` field     |
| `POST`   | `/situations/{id}/dismiss`            | Mark situation as dismissed. Optional `{"reason": "..."}` body stores a dismiss reason |
| `POST`   | `/situations/{id}/undismiss`          | Restore a previously dismissed situation to `new`                                  |
| `POST`   | `/situations/{id}/transition`         | Transition lifecycle to `{new, investigating, waiting, resolved, dismissed}`. Body: `{"to_status": "...", "note": "...", "follow_up_date": "..."}`. Logs a `situation_events` row. |
| `POST`   | `/situations/{id}/rescore`            | Manually trigger score recomputation and LLM re-synthesis for a situation          |
| `POST`   | `/situations/{id}/split`              | Move a subset of items out of the source situation into a new one. Body: `{"item_ids": [...], "new_title": "<optional>"}`. Returns `{"ok": true, "new_situation_id": "...", "original_situation_id": "..."}`. Both situations are lightweight-rescored (no LLM); the source cannot be emptied. |
| `POST`   | `/situations/{id}/merge`              | Merge another situation into this one. Body: `{"source_situation_id": "..."}`. Source items are relinked to the target and the source is dismissed with `dismiss_reason="merged_into:<target>"`. |
| `PATCH`  | `/situations/{id}`                    | Update `title`, `status`, `follow_up_date`, `notes`, or `project_tag`. Status changes are logged in `situation_events`. |
| `GET`    | `/situations/{id}/events`             | Return the status transition history for a situation                               |
| `POST`   | `/situations/{id}/deep-analysis`      | Submit the situation for extended-context deep analysis via the merLLM background batch queue. Returns `{"ok": true, "job_id": "..."}`. Prompt includes situation title, summary, all item summaries, and open actions. |
| `POST`   | `/situations/{id}/deep-analysis/save` | Fetch completed batch result and store it as an intel item linked to the situation. Body: `{"job_id": "..."}`. Returns 409 if the job is not yet complete. |

### Batch status proxy

| Method | Path                    | Description                                                      |
|--------|-------------------------|------------------------------------------------------------------|
| `GET`  | `/batch/status/{job_id}` | Proxy `GET /api/batch/status/{job_id}` to merLLM. Returns the job record or 404. |

### Passdown

| Method | Path                 | Description                                                                                                 |
|--------|----------------------|-------------------------------------------------------------------------------------------------------------|
| `POST` | `/passdown/generate` | Assemble a shift-handoff HTML email. Body: `{"hours": <1..168>}`. Returns `{html, sections, generated_at, hours}`. See [Passdown generator](#passdown-generator). |

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
| `GET`    | `/slack/connect`              | Redirect to Slack OAuth authorisation page (includes CSRF `state` nonce) |
| `GET`    | `/slack/callback`             | OAuth callback — validates `state`, exchanges code for user token, encrypts and saves to settings |
| `GET`    | `/slack/workspaces`           | List connected workspaces (team name and ID)                       |
| `DELETE` | `/slack/workspaces/{team_id}` | Disconnect a workspace                                             |

### Microsoft Teams OAuth

| Method   | Path                             | Description                                                              |
|----------|----------------------------------|--------------------------------------------------------------------------|
| `GET`    | `/teams/connect`                 | Redirect to Microsoft identity platform OAuth authorisation page (includes CSRF `state` nonce) |
| `GET`    | `/teams/callback`                | OAuth callback — validates `state`, exchanges code for user token, encrypts and saves to settings |
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

### Attention model

| Method | Path                  | Description                                                              |
|--------|-----------------------|--------------------------------------------------------------------------|
| `GET`  | `/attention/summary`  | Attention model summary for the merLLM "My Day" panel: situation counts, overdue follow-ups, high-attention items |

### Noise filters

| Method   | Path                    | Description                                                            |
|----------|-------------------------|------------------------------------------------------------------------|
| `GET`    | `/noise-filters`        | List all pre-scan noise filter rules                                   |
| `POST`   | `/noise-filters`        | Add a noise filter rule (`{"type": "sender_contains", "value": "..."}`) |
| `DELETE` | `/noise-filters/{index}` | Remove a noise filter rule by index                                   |

### Contacts

The contacts table is a long-lived directory of every person Parsival has seen across email headers, with full manual editing. Identity is a stable serial integer (`contact_id`), **not** an email address — people change addresses when they switch jobs, but the contact record outlives any single field. Multiple emails can belong to one contact via the `contact_emails` join table. Owner-resolution falls back to this table when an LLM-identified delegate isn't named in the current item's To/CC headers.

| Method   | Path                                       | Description                                                                                       |
|----------|--------------------------------------------|---------------------------------------------------------------------------------------------------|
| `GET`    | `/contacts`                                | List contacts (params: `query` = substring match against name/employer/title/email, `limit`)     |
| `GET`    | `/contacts/{id}`                           | Fetch one contact with all attached emails                                                       |
| `POST`   | `/contacts`                                | Manually create a contact. Body: `{"name", "phone", "employer", "title", "employer_address", "notes", "emails": ["..."]}` — first email becomes primary |
| `PATCH`  | `/contacts/{id}`                           | Update any subset of contact fields                                                              |
| `DELETE` | `/contacts/{id}`                           | Delete a contact and cascade-remove all attached emails                                          |
| `POST`   | `/contacts/{id}/emails`                    | Attach an email to a contact (`{"email", "is_primary"}`). Returns 409 if email belongs to another contact |
| `DELETE` | `/contacts/{id}/emails/{email}`            | Detach an email from a contact                                                                   |
| `POST`   | `/contacts/rebuild`                        | Rescan every existing item's `author`/`to`/`cc` headers and refresh the contacts table. Idempotent — safe to run repeatedly after schema changes or bulk imports |
| `POST`   | `/contacts/reparse-signatures`             | Walk every item and re-run the email-body signature parser. Extracts phone, title, employer, and address from sender footers. Manually-edited fields are never overwritten. Idempotent |

Live ingestion is automatic: every time the analysis pipeline saves an item, both passes run in the background — header scraping populates the row from To/CC/author, then the signature parser enriches it from the email body. Failures in either pass are swallowed so they never block analysis.

**Provenance and manual edits.** Each editable field on a contact carries a `*_source` tag of `header`, `signature`, or `manual`. The signature parser tags every field it writes with the corresponding confidence score (stored in `signature_confidence`) and **never** overwrites a field that has been edited by hand — those are tracked in `manually_edited_fields` and the editor flips them to `manual` automatically when you save. Re-running `/contacts/reparse-signatures` is therefore safe: it will fill in gaps from new emails and refresh confidence scores, but it cannot clobber your edits.

### Look-ahead

See [Look-ahead board](#look-ahead-board) for an overview of the feature. All endpoints return JSON.

| Method   | Path                                              | Description                                                                                       |
|----------|---------------------------------------------------|---------------------------------------------------------------------------------------------------|
| `GET`    | `/lookahead/cards`                                | List cards (params: `project`, `start`, `end` — date overlap filter). Each card includes inlined `depends_on`, `links`, and `resources` |
| `GET`    | `/lookahead/cards/{id}`                           | Fetch a single card with all relations                                                            |
| `POST`   | `/lookahead/cards`                                | Create a card. Required: `title`, `project`, `start_date`, `start_shift_num`, `end_date`, `end_shift_num`. Optional: `assignee`, `status`, `depends_on`, `links`, `resources`. Returns the created card with a generated UUID |
| `PATCH`  | `/lookahead/cards/{id}`                           | Update any subset of card fields. Passing `depends_on`, `links`, or `resources` replaces the relation set; self-dependencies are silently dropped |
| `DELETE` | `/lookahead/cards/{id}`                           | Delete a card. Cascades through `depends_on`, `links`, and BOM rows                               |
| `PATCH`  | `/lookahead/cards/{card_id}/resources/{res_id}`   | Toggle a single BOM row's status (`needed` / `secured` / `consumed`) without rewriting the whole resource list |
| `GET`    | `/lookahead/resources`                            | List the resource catalog (param: `type`)                                                         |
| `POST`   | `/lookahead/resources`                            | Create a resource (`{"name", "type", "notes"}`). Types: `person`, `equipment`, `space`, `part`, `supply` |
| `PATCH`  | `/lookahead/resources/{id}`                       | Update a resource                                                                                 |
| `DELETE` | `/lookahead/resources/{id}`                       | Delete a resource and cascade-remove its BOM references                                           |
| `GET`    | `/lookahead/shifts`                               | List shift schedules (param: `project`)                                                           |
| `PUT`    | `/lookahead/shifts/{project}/{shift_num}`         | Upsert a shift (1..6). Body: `{"label", "start_time", "end_time", "days"}` — times are `HH:MM`, days a comma-separated mask like `M,T,W,Th,F` |
| `DELETE` | `/lookahead/shifts/{project}/{shift_num}`         | Delete a single shift row                                                                         |
| `GET`    | `/lookahead/overview`                             | Cross-project rollup: one row per project sorted by earliest card start, each with its full card list |
| `GET`    | `/lookahead/templates`                            | List templates (param: `owner`). Each response inlines `tasks`, `depends_on`, and `resource_requirements` |
| `GET`    | `/lookahead/templates/{id}`                       | Fetch a single template with its task graph                                                       |
| `POST`   | `/lookahead/templates`                            | Create a template. Body: `{"name", "description", "owner", "duration_unit", "default_project_tag", "tasks": [...]}`. Each task needs `local_id` and may carry `depends_on` + `resource_requirements` |
| `PATCH`  | `/lookahead/templates/{id}`                       | Update template fields. Passing `tasks` replaces the whole task graph. `version` is bumped on every PATCH |
| `DELETE` | `/lookahead/templates/{id}`                       | Delete a template. Existing instances keep their cards; cards' `template_instance_id` still points at the orphaned instance row |
| `POST`   | `/lookahead/templates/{id}/instantiate`           | Materialise an instance. Body: `{"start_date", "project_tag", "owner"}`. Returns `{instance, cards}` |
| `GET`    | `/lookahead/instances`                            | List instances (params: `project`, `status` — `active` / `complete` / `cancelled`). Each row carries `template_version`, `template_current_version`, and `outdated` so the UI can offer per-instance upgrades |
| `GET`    | `/lookahead/instances/{id}`                       | Fetch an instance plus its attached cards                                                         |
| `PATCH`  | `/lookahead/instances/{id}`                       | Reschedule the instance (`{"start_date"}`) or flip status. Reschedule shifts every attached card by the same delta |
| `DELETE` | `/lookahead/instances/{id}`                       | Delete the instance and cascade-remove still-attached cards. Detached cards are left untouched    |
| `POST`   | `/lookahead/instances/{id}/upgrade`               | Opt-in: re-apply the latest template version to the instance. Refreshes template-derived fields on existing cards, creates cards for newly-added tasks, preserves user edits (assignee, status, notes, added BOM rows). No-op if already current |
| `POST`   | `/lookahead/cards/{id}/detach`                    | Remove a card from its template instance without deleting it. Future reschedules of the instance no longer move this card |
| `GET`    | `/lookahead/cards/{id}/suggestions`               | List pending cross-system link suggestions for a card. Item suggestions include `target_title`, `target_source`, `target_url` |
| `POST`   | `/lookahead/cards/{id}/annotate`                  | Run the LLM annotator synchronously for one card — surfaces related items from the same project as pending suggestions |
| `POST`   | `/lookahead/annotate-project`                     | Bulk-run the annotator across every card in the given project + window. Body: `{"project", "start", "end"}` |
| `POST`   | `/lookahead/suggestions/{id}/accept`              | Accept a suggestion — graduates it into a concrete `lookahead_card_links` row                     |
| `POST`   | `/lookahead/suggestions/{id}/reject`              | Reject a suggestion — it stops showing up in the pending list but is remembered so the annotator won't re-propose it |

## Request logging

All HTTP requests are logged via middleware: timestamp, method, path, status code, duration (ms), and user email (from `CF-Access-Authenticated-User-Email` header, or `anonymous`). Log levels: INFO for 2xx/3xx, WARNING for 4xx, ERROR for 5xx.

## Data persistence

Stored in `./data/parsival.db` (SQLite WAL). Bind-mounted and survives restarts.

```bash
docker compose down
rm data/parsival.db   # wipe all data
docker compose up -d
```

Alternatively, use `POST /reset` to clear analyses, todos, and scan logs while keeping your saved settings.

### Database backup

Back up `parsival.db` while the container is running using the SQLite CLI's
online backup command (safe with WAL mode):

```bash
sqlite3 data/parsival.db ".backup 'backups/parsival-$(date +%Y%m%d-%H%M%S).db'"
```

**Scheduled backup via cron (run on the host):**

```bash
# Back up every night at 03:00
0 3 * * * sqlite3 /opt/hexcaliper-parsival/data/parsival.db \
  ".backup '/opt/hexcaliper-parsival/backups/parsival-$(date +%Y%m%d-%H%M%S).db'" \
  >> /var/log/parsival-backup.log 2>&1
```

Or with the helper script (if present):

```bash
0 3 * * * /opt/hexcaliper-parsival/backup_db.sh >> /var/log/parsival-backup.log 2>&1
```

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

### Background batch processing (merLLM integration)

When `POST /reanalyze` is triggered, Parsival submits each re-analysis job to `POST {MERLLM_URL}/api/batch/submit` instead of calling Ollama directly. merLLM enqueues the work in its `background` priority bucket, where strict top-down draining guarantees `chat`/`short`/`feedback` traffic always preempts bulk reanalysis at the dispatcher.

Each submitted job stores a `batch_job_id` on the item record. A background polling thread (60-second interval) checks `GET {MERLLM_URL}/api/batch/result/{job_id}` for each pending job, parses the completed response, and applies the result exactly as a direct analysis would — updating the analysis record, todos, intel, knowledge graph, and situation formation.

If merLLM is unreachable, re-analysis proceeds with direct Ollama calls as normal. If a batch submission fails for an individual item, that item falls back to direct Ollama automatically.

Set the `MERLLM_URL` environment variable (default: `http://host.docker.internal:11400`) to point at your merLLM instance.
