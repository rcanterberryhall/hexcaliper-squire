# Hexcaliper Squire

A companion service for [Hexcaliper](https://github.com/rcanterberryhall/hexcaliper) that consolidates responsibilities from Outlook, Slack, GitHub, and Jira into a single ops dashboard. Uses the Hexcaliper Ollama instance to extract action items — no data leaves your infrastructure.

## Architecture

```
Browser (/page/)
  └── nginx (:8082)
        └── /page/api/* → FastAPI/uvicorn (:8001, service: page-api)
                            ├── Ollama (hexcaliper.com via Cloudflare Access)
                            ├── Slack API
                            ├── GitHub API
                            ├── Jira Cloud API
                            └── TinyDB  (./data/page.db)

Email ingestion (host, not Docker):
  Windows  → scripts/outlook_sidecar.py      (win32com)
  Ubuntu   → scripts/thunderbird_sidecar.py  (local mbox/Maildir)
    └── POST /page/api/ingest → API
```

Runs alongside the existing Hexcaliper stack. Does not conflict with hexcaliper's ports (8080/8000).

| Port | Service |
|------|---------|
| 8001 | FastAPI API (internal, bridge network) |
| 8082 | nginx (web UI + API proxy, host-exposed) |

| Component | Technology |
|-----------|-----------|
| Frontend  | Vanilla JS + CSS, served by nginx |
| API       | Python 3.12, FastAPI, uvicorn |
| Storage   | TinyDB (flat JSON — same as Hexcaliper) |
| LLM       | Ollama via Hexcaliper appliance (Cloudflare Access) |
| Networking | Bridge (`app` network) — same pattern as Hexcaliper |

## Connectors

| Source   | How | What it pulls |
|----------|-----|--------------|
| Outlook  | Host sidecar script | Recent inbox emails |
| Slack    | Bot token REST API | DMs and channel mentions |
| GitHub   | PAT REST API | Notifications, assigned issues, PR review requests |
| Jira     | API token REST API | Open tickets assigned to current user |

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

All credentials are set directly in `docker-compose.yml` under the `page-api` environment block (no `.env` file required).

| Variable | Description |
|----------|-------------|
| `CF_CLIENT_ID` | Cloudflare Access service token ID |
| `CF_CLIENT_SECRET` | Cloudflare Access service token secret |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-...`) |
| `SLACK_CHANNELS` | Comma-separated channel names. Empty = all joined channels |
| `GITHUB_PAT` | GitHub PAT (scopes: `repo`, `notifications`) |
| `GITHUB_USERNAME` | Your GitHub username |
| `JIRA_EMAIL` | Jira account email |
| `JIRA_TOKEN` | Jira API token |
| `JIRA_DOMAIN` | `yourco.atlassian.net` |
| `JIRA_JQL` | JQL for your tickets |
| `LOOKBACK_HOURS` | Hours of history per scan (default: `48`) |
| `OLLAMA_MODEL` | Model for extraction (default: `llama3.2`) |

### Cloudflare Access service token
Zero Trust → **Access → Service Auth → Service Tokens** → Create token.
Copy Client ID and Client Secret (shown once). Add the token to the Access Policy protecting your Ollama application.

### Slack bot token
Required scopes: `channels:history` `channels:read` `groups:history` `groups:read` `im:history` `mpim:history` `users:read`

### GitHub PAT
Required scopes: `repo` `notifications`

### Jira API token
https://id.atlassian.com/manage-profile/security/api-tokens

## Email ingestion (sidecar scripts)

Email is fed into the API from the host machine — both Outlook (win32com) and Thunderbird (local mbox) require local client state not available inside Docker.

### Windows — Outlook

```bash
pip install requests pywin32
python scripts/outlook_sidecar.py
```

Schedule with Windows Task Scheduler to run every 30–60 minutes.

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

## API reference

Interactive docs: `http://localhost:8001/docs`

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | Health check and config warnings |
| `POST` | `/scan` | Start a scan (`{"sources": ["slack","github","jira","outlook"]}`) |
| `GET`  | `/scan/status` | Poll scan progress |
| `POST` | `/ingest` | Receive items from sidecar scripts |
| `GET`  | `/todos` | List action items (params: `source`, `priority`, `done`) |
| `PATCH`| `/todos/{id}` | Update a todo (`{"done": true}`) |
| `DELETE`| `/todos/{id}` | Delete a todo |
| `GET`  | `/analyses` | All analyzed items (params: `source`, `category`) |
| `GET`  | `/stats` | Aggregate counts and last scan info |

## Data persistence

Stored in `./data/page.db` (TinyDB JSON). Bind-mounted and survives restarts.

```bash
docker compose down
rm data/page.db   # reset all data
```
