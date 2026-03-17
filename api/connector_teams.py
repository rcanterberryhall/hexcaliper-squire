"""
connector_teams.py — Microsoft Teams data connector.

Fetches @mentions, direct messages, and joined-team channel activity for all
connected accounts using per-user OAuth tokens obtained via the Microsoft Graph
Authorization Code flow.

When ``config.PROJECTS`` or ``config.FOCUS_TOPICS`` are configured, channel
messages are pre-filtered by ``_relevance()`` before being turned into
``RawItem`` objects.  ``_relevance()`` checks (in priority order):

1. User name / email text patterns → ``"user"``
2. Project keywords (manual + learned) → ``"project"``
3. Watch-topic keywords → ``"topic"``
4. Noise keywords (only reached if no positive match) → skip

Each call to ``fetch()`` returns a deduplicated list of ``RawItem`` objects
covering the lookback window defined in ``config.LOOKBACK_HOURS``.
"""
import requests
from datetime import datetime, timedelta, timezone
from models import RawItem
import config

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


# ── Token management ──────────────────────────────────────────────────────────

def _refresh_token(ws: dict) -> str | None:
    """
    Exchange a refresh token for a fresh access token.

    Updates the ``ws`` dict in-place with the new ``access_token`` and
    ``refresh_token`` (Microsoft rotates refresh tokens).

    :param ws: Workspace token dict with ``refresh_token``, ``client_id``, ``client_secret``.
    :return: Fresh access token string, or ``None`` if refresh fails.
    """
    refresh = ws.get("refresh_token")
    client_id = config.TEAMS_CLIENT_ID
    client_secret = config.TEAMS_CLIENT_SECRET
    if not refresh or not client_id or not client_secret:
        return None
    try:
        r = requests.post(TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh,
            "client_id":     client_id,
            "client_secret": client_secret,
            "scope":         "Chat.Read ChannelMessage.Read.All Channel.ReadBasic.All offline_access",
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "access_token" in data:
            ws["access_token"] = data["access_token"]
        if "refresh_token" in data:
            ws["refresh_token"] = data["refresh_token"]
        return ws.get("access_token")
    except Exception as e:
        print(f"[teams] token refresh failed: {e}")
        return None


def _get(token: str, path: str, params: dict = None) -> dict:
    """
    Make an authenticated GET request to the Microsoft Graph API.

    :param token: A valid Graph API access token.
    :param path: Graph endpoint path, e.g. ``"/me/chats"``.
    :param params: Optional query parameters.
    :return: Parsed JSON response.
    :raises requests.HTTPError: If the HTTP request fails.
    """
    r = requests.get(
        f"{GRAPH}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _paged(token: str, path: str, params: dict = None) -> list:
    """Collect all pages of a Graph API list response."""
    results = []
    url = f"{GRAPH}{path}"
    while url:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        params = None  # already encoded in nextLink
    return results


# ── Relevance filtering ───────────────────────────────────────────────────────

def _user_identifiers() -> list[str]:
    ids = []
    if config.USER_NAME:
        ids.append(config.USER_NAME.lower())
    if config.USER_EMAIL:
        email = config.USER_EMAIL.lower()
        ids.append(email)
        username = email.split("@")[0]
        if username:
            ids.append("@" + username)
    return ids


def _relevance(text: str) -> tuple[bool, str, str | None]:
    """
    Determine whether a Teams message is relevant to the configured user context.

    :return: ``(relevant, hierarchy, project_tag)``
    """
    lower = text.lower()

    for ident in _user_identifiers():
        if ident in lower:
            return True, "user", None

    for p in config.PROJECTS:
        all_kw = list(p.get("keywords", [])) + list(p.get("learned_keywords", []))
        for kw in all_kw:
            if kw.lower() in lower:
                return True, "project", p["name"]

    for t in config.FOCUS_TOPICS:
        if t.lower() in lower:
            return True, "topic", None

    for kw in config.NOISE_KEYWORDS:
        if kw.lower() in lower:
            return False, "noise", None

    return False, "general", None


# ── Per-token fetch ───────────────────────────────────────────────────────────

def _parse_ts(ts_str: str | None) -> float:
    """Parse a Graph API ISO timestamp to a UTC Unix float."""
    if not ts_str:
        return 0.0
    try:
        # Graph returns e.g. "2026-03-15T10:00:00.000Z" or with offset
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _body_text(msg: dict) -> str:
    """Extract plain text from a Graph message body dict."""
    b = msg.get("body", {})
    content = b.get("content", "")
    # Strip basic HTML tags for contentType=html messages
    if b.get("contentType") == "html":
        import re
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"\s+", " ", content).strip()
    return content


def _fetch_for_token(ws: dict, cutoff_ts: float) -> list[RawItem]:
    """
    Fetch @mentions, DMs, and joined-team channel activity for one user token.

    Three passes:
    1. ``/me/messages`` — @mention activity items across all teams.
    2. ``/me/chats`` — 1:1 and group DMs.
    3. ``/me/joinedTeams`` + ``/teams/{id}/channels`` — channel messages,
       filtered through ``_relevance()`` when projects/topics are configured.

    :param ws: Workspace dict with ``access_token``, ``refresh_token``, ``display_name``.
    :param cutoff_ts: Unix timestamp for earliest message to include.
    :return: Deduplicated list of ``RawItem`` objects from this account.
    """
    token = ws.get("access_token", "")
    if not token:
        return []

    items: list[RawItem] = []
    seen:  set[str]      = set()

    # Identify the token owner
    try:
        me = _get(token, "/me")
        my_id    = me.get("id", "")
        my_name  = me.get("displayName", "me")
        tenant   = me.get("userPrincipalName", "").split("@")[-1] or "teams"
    except Exception as e:
        # Try refreshing the token once
        token = _refresh_token(ws) or ""
        if not token:
            print(f"[teams] /me failed and refresh failed: {e}")
            return []
        try:
            me = _get(token, "/me")
            my_id   = me.get("id", "")
            my_name = me.get("displayName", "me")
            tenant  = me.get("userPrincipalName", "").split("@")[-1] or "teams"
        except Exception as e2:
            print(f"[teams] /me failed after refresh: {e2}")
            return []

    cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[teams:{tenant}] my_id={my_id}, cutoff={cutoff_iso}")

    # ── 1. @mentions via activity feed ────────────────────────────────────────
    try:
        feed = _paged(token, "/me/messages", {
            "$top": 50,
            "$filter": f"createdDateTime ge {cutoff_iso}",
            "$orderby": "createdDateTime desc",
        })
        print(f"[teams:{tenant}] activity feed: {len(feed)} messages")
        for msg in feed:
            ts = _parse_ts(msg.get("createdDateTime"))
            if ts < cutoff_ts:
                continue
            mid = f"mention_{my_id}_{msg.get('id', ts)}"
            if mid in seen:
                continue
            seen.add(mid)
            text = _body_text(msg)
            sender = (msg.get("from") or {}).get("user", {}).get("displayName", "?")
            items.append(RawItem(
                source    = "teams",
                item_id   = mid,
                title     = f"[@mention] ({tenant}): {text[:80]}",
                body      = text[:3000],
                url       = (msg.get("webUrl") or ""),
                author    = sender,
                timestamp = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                metadata  = {"tenant": tenant, "type": "mention"},
            ))
    except Exception as e:
        print(f"[teams:{tenant}] activity feed: {e}")

    # ── 2. Direct messages and group chats ────────────────────────────────────
    try:
        chats = _paged(token, "/me/chats", {
            "$top": 50,
            "$expand": "members",
        })
        print(f"[teams:{tenant}] chats found: {len(chats)}")
        for chat in chats:
            chat_id   = chat.get("id", "")
            chat_type = chat.get("chatType", "")
            try:
                msgs = _paged(token, f"/me/chats/{chat_id}/messages", {
                    "$top": 10,
                    "$orderby": "createdDateTime desc",
                })
            except Exception:
                continue
            if not msgs:
                continue

            # Build a single RawItem per chat with recent context
            lines = []
            latest_ts = 0.0
            for msg in reversed(msgs[:10]):
                if msg.get("messageType") != "message":
                    continue
                sender = (msg.get("from") or {}).get("user", {}).get("displayName", "?")
                text = _body_text(msg)
                lines.append(f"[{sender}]: {text}")
                ts = _parse_ts(msg.get("createdDateTime"))
                if ts > latest_ts:
                    latest_ts = ts

            if not lines or not latest_ts:
                continue

            mid = f"chat_{chat_id}_{latest_ts:.0f}"
            if mid in seen:
                continue
            seen.add(mid)
            # Label: oneOnOne → "DM", group → "Group chat"
            label = "DM" if chat_type == "oneOnOne" else "Group"
            members = chat.get("members", [])
            other_names = [
                m.get("displayName", "")
                for m in members
                if m.get("displayName") and m.get("displayName") != my_name
            ]
            title_names = ", ".join(other_names[:3]) or "?"
            items.append(RawItem(
                source    = "teams",
                item_id   = mid,
                title     = f"[{label}] ({tenant}) w/ {title_names}: {lines[-1][:60]}",
                body      = "\n".join(lines)[:3000],
                url       = chat.get("webUrl") or f"https://teams.microsoft.com/l/chat/{chat_id}/",
                author    = other_names[0] if other_names else my_name,
                timestamp = datetime.fromtimestamp(latest_ts, tz=timezone.utc).isoformat(),
                metadata  = {"tenant": tenant, "type": "dm" if chat_type == "oneOnOne" else "group_chat"},
            ))
    except Exception as e:
        print(f"[teams:{tenant}] chats: {e}")

    # ── 3. Joined teams → channel messages ───────────────────────────────────
    try:
        joined = _paged(token, "/me/joinedTeams")
        print(f"[teams:{tenant}] joined teams: {len(joined)}")
        filtering = bool(config.PROJECTS or config.FOCUS_TOPICS)

        for team in joined:
            team_id   = team.get("id", "")
            team_name = team.get("displayName", team_id)
            try:
                channels = _paged(token, f"/teams/{team_id}/channels")
            except Exception:
                continue

            for ch in channels:
                ch_id   = ch.get("id", "")
                ch_name = ch.get("displayName", ch_id)
                try:
                    msgs = _paged(token, f"/teams/{team_id}/channels/{ch_id}/messages", {
                        "$top": 50 if filtering else 20,
                        "$filter": f"createdDateTime ge {cutoff_iso}",
                    })
                except Exception:
                    continue

                if not msgs:
                    continue

                ch_hierarchy = "general"
                ch_project   = None
                if filtering:
                    relevant = []
                    for msg in msgs:
                        if msg.get("messageType") != "message":
                            continue
                        text = _body_text(msg)
                        # Check if message mentions the user
                        mentions = msg.get("mentions", [])
                        is_mention = any(
                            m.get("mentioned", {}).get("user", {}).get("id") == my_id
                            for m in mentions
                        )
                        if is_mention:
                            relevant.append(msg)
                            ch_hierarchy = "user"
                            continue
                        ok, h, pt = _relevance(text)
                        if ok:
                            relevant.append(msg)
                            if h == "user" or ch_hierarchy == "general":
                                ch_hierarchy = h
                            if pt and not ch_project:
                                ch_project = pt
                    msgs = relevant
                    if not msgs:
                        continue
                else:
                    msgs = [m for m in msgs if m.get("messageType") == "message"]
                    if not msgs:
                        continue

                print(f"[teams:{tenant}] {team_name}/#{ch_name}: {len(msgs)} msgs — including")

                lines = []
                latest_ts = 0.0
                for msg in reversed(msgs):
                    sender = (msg.get("from") or {}).get("user", {}).get("displayName", "?")
                    text = _body_text(msg)
                    lines.append(f"[{sender}]: {text}")
                    ts = _parse_ts(msg.get("createdDateTime"))
                    if ts > latest_ts:
                        latest_ts = ts

                if not lines or not latest_ts:
                    continue

                mid = f"ch_{ch_id}_{latest_ts:.0f}"
                if mid in seen:
                    continue
                seen.add(mid)
                items.append(RawItem(
                    source    = "teams",
                    item_id   = mid,
                    title     = f"[#{ch_name}] {team_name} ({tenant}): recent activity",
                    body      = "\n".join(lines)[:3000],
                    url       = ch.get("webUrl") or f"https://teams.microsoft.com",
                    author    = f"#{ch_name}",
                    timestamp = datetime.fromtimestamp(latest_ts, tz=timezone.utc).isoformat(),
                    metadata  = {
                        "channel":     ch_name,
                        "team":        team_name,
                        "tenant":      tenant,
                        "type":        "channel",
                        "hierarchy":   ch_hierarchy,
                        "project_tag": ch_project,
                    },
                ))
    except Exception as e:
        print(f"[teams:{tenant}] joined teams/channels: {e}")

    print(f"[teams:{tenant}] {len(items)} items")
    return items


# ── Public entry point ────────────────────────────────────────────────────────

def fetch() -> list[RawItem]:
    """
    Fetch Teams items across all connected accounts.

    :return: Combined list of raw items from all accounts, deduplicated
             within each account by item ID.
    :rtype: list[RawItem]
    """
    if not config.TEAMS_USER_TOKENS:
        print("[teams] not configured — skipping")
        return []

    cutoff_ts = (
        datetime.now(timezone.utc) - timedelta(hours=config.LOOKBACK_HOURS)
    ).timestamp()

    all_items: list[RawItem] = []
    for ws in config.TEAMS_USER_TOKENS:
        try:
            all_items.extend(_fetch_for_token(ws, cutoff_ts))
        except Exception as e:
            print(f"[teams] account {ws.get('display_name', '?')}: {e}")
    return all_items
