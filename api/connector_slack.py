import requests
from datetime import datetime, timedelta, timezone
from models import RawItem
import config

BASE = "https://slack.com/api"


def _get(token: str, endpoint: str, params: dict = None) -> dict:
    r = requests.get(
        f"{BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack error on {endpoint}: {data.get('error')}")
    return data


def _username(token: str, uid: str, cache: dict) -> str:
    if uid in cache:
        return cache[uid]
    try:
        name = _get(token, "users.info", {"user": uid})["user"].get("real_name") or uid
    except Exception:
        name = uid
    cache[uid] = name
    return name


def _fetch_for_token(token: str, cutoff_ts: float) -> list[RawItem]:
    """Fetch @mentions, DMs, and active channel threads for one user token."""
    items: list[RawItem] = []
    seen:  set[str]      = set()
    cache: dict          = {}

    # Identify whose token this is
    try:
        auth   = _get(token, "auth.test")
        my_uid = auth.get("user_id", "")
        team   = auth.get("team", "")
    except Exception as e:
        print(f"[slack] auth.test failed: {e}")
        return []

    print(f"[slack:{team}] my_uid={my_uid}, cutoff={datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()}")

    # ── 1. @mentions via search API ──────────────────────────────────────────
    try:
        search_result = _get(token, "search.messages", {
            "query":    f"<@{my_uid}>",
            "count":    20,
            "sort":     "timestamp",
            "sort_dir": "desc",
        })
        matches = search_result.get("messages", {}).get("matches", [])
        print(f"[slack:{team}] mentions search: {len(matches)} total matches")

        for m in matches:
            ts = float(m.get("ts", 0))
            if ts < cutoff_ts:
                continue
            mid = f"mention_{my_uid}_{m['ts']}"
            if mid in seen:
                continue
            seen.add(mid)
            ch = m.get("channel", {})
            items.append(RawItem(
                source    = "slack",
                item_id   = mid,
                title     = f"[@mention] #{ch.get('name','?')} ({team}): {m.get('text','')[:80]}",
                body      = m.get("text", "")[:3000],
                url       = m.get("permalink", f"https://slack.com/app_redirect?channel={ch.get('id','')}"),
                author    = _username(token, m.get("user", "?"), cache),
                timestamp = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                metadata  = {"channel": ch.get("name", ""), "workspace": team, "type": "mention"},
            ))
    except Exception as e:
        print(f"[slack:{team}] mentions: {e}")

    # ── 2. Direct messages and group DMs ─────────────────────────────────────
    # For DMs we don't filter by cutoff — always surface the most recent thread
    try:
        channels = _get(token, "conversations.list", {
            "types":            "im,mpim",
            "exclude_archived": True,
            "limit":            50,
        }).get("channels", [])
        print(f"[slack:{team}] DM conversations found: {len(channels)}")

        for ch in channels:
            ch_id = ch["id"]
            try:
                msgs = _get(token, "conversations.history", {
                    "channel": ch_id,
                    "limit":   10,          # most recent messages, no time filter
                }).get("messages", [])
            except Exception:
                continue

            if not msgs:
                continue

            # Build a single RawItem per DM conversation with full context
            lines = []
            for msg in reversed(msgs):
                sender = _username(token, msg.get("user", "?"), cache)
                lines.append(f"[{sender}]: {msg.get('text', '')}")

            mid = f"dm_{ch_id}_{msgs[0]['ts']}"
            if mid in seen:
                continue
            seen.add(mid)
            first_ts = float(msgs[0]["ts"])
            items.append(RawItem(
                source    = "slack",
                item_id   = mid,
                title     = f"[DM] ({team}): {msgs[0].get('text','')[:70]}",
                body      = "\n".join(lines)[:3000],
                url       = f"https://slack.com/app_redirect?channel={ch_id}",
                author    = _username(token, msgs[0].get("user", "?"), cache),
                timestamp = datetime.fromtimestamp(first_ts, tz=timezone.utc).isoformat(),
                metadata  = {"workspace": team, "type": "dm"},
            ))
    except Exception as e:
        print(f"[slack:{team}] DMs: {e}")

    # ── 3. Channels where I participated or was mentioned ────────────────────
    try:
        channels = _get(token, "conversations.list", {
            "types":            "public_channel,private_channel",
            "exclude_archived": True,
            "limit":            200,
        }).get("channels", [])
        print(f"[slack:{team}] channel memberships: {len(channels)}")

        for ch in channels:
            ch_id   = ch["id"]
            ch_name = ch.get("name", ch_id)

            try:
                msgs = _get(token, "conversations.history", {
                    "channel": ch_id,
                    "oldest":  str(cutoff_ts),
                    "limit":   40,
                }).get("messages", [])
            except Exception:
                continue

            if not msgs:
                continue

            print(f"[slack:{team}] #{ch_name}: {len(msgs)} msgs — including")

            lines = []
            for msg in reversed(msgs):
                sender = _username(token, msg.get("user", "?"), cache)
                lines.append(f"[{sender}]: {msg.get('text', '')}")

            mid = f"ch_{ch_id}_{msgs[0]['ts']}"
            if mid in seen:
                continue
            seen.add(mid)
            first_ts = float(msgs[0]["ts"])
            items.append(RawItem(
                source    = "slack",
                item_id   = mid,
                title     = f"[#{ch_name}] ({team}): recent activity",
                body      = "\n".join(lines)[:3000],
                url       = f"https://slack.com/app_redirect?channel={ch_id}",
                author    = f"#{ch_name}",
                timestamp = datetime.fromtimestamp(first_ts, tz=timezone.utc).isoformat(),
                metadata  = {"channel": ch_name, "workspace": team, "type": "channel"},
            ))
    except Exception as e:
        print(f"[slack:{team}] channels: {e}")

    print(f"[slack:{team}] {len(items)} items")
    return items


def fetch() -> list[RawItem]:
    cutoff_ts = (
        datetime.now(timezone.utc) - timedelta(hours=config.LOOKBACK_HOURS)
    ).timestamp()

    # ── User token path (one per connected workspace) ─────────────────────────
    if config.SLACK_USER_TOKENS:
        all_items: list[RawItem] = []
        for ws in config.SLACK_USER_TOKENS:
            token = ws.get("token", "")
            if not token:
                continue
            try:
                all_items.extend(_fetch_for_token(token, cutoff_ts))
            except Exception as e:
                print(f"[slack] workspace {ws.get('team','?')}: {e}")
        return all_items

    # ── Legacy bot token fallback ─────────────────────────────────────────────
    if not config.SLACK_BOT_TOKEN or config.SLACK_BOT_TOKEN.startswith("xoxb-your"):
        print("[slack] not configured — skipping")
        return []

    print("[slack] using legacy bot token")
    token      = config.SLACK_BOT_TOKEN
    cutoff_str = str(cutoff_ts)
    cache: dict          = {}
    items: list[RawItem] = []

    try:
        bot_uid  = _get(token, "auth.test").get("user_id", "")
        channels = _get(token, "conversations.list", {
            "types":            "public_channel,private_channel,im,mpim",
            "exclude_archived": True,
            "limit":            100,
        }).get("channels", [])

        if config.SLACK_CHANNELS:
            name_map = {c["name"]: c for c in channels}
            channels = [name_map[n] for n in config.SLACK_CHANNELS if n in name_map]

        for ch in channels:
            ch_id   = ch["id"]
            ch_name = ch.get("name", ch_id)
            is_im   = ch.get("is_im", False)
            try:
                msgs = _get(token, "conversations.history", {
                    "channel": ch_id,
                    "oldest":  cutoff_str,
                    "limit":   50,
                }).get("messages", [])
            except Exception as e:
                print(f"[slack] #{ch_name}: {e}")
                continue

            for msg in msgs:
                text = msg.get("text", "")
                if not (is_im or f"<@{bot_uid}>" in text):
                    continue
                body = text
                if msg.get("reply_count", 0) > 0:
                    try:
                        replies = _get(token, "conversations.replies", {"channel": ch_id, "ts": msg["ts"]})
                        for rp in replies.get("messages", [])[1:5]:
                            rn   = _username(token, rp.get("user", "?"), cache)
                            body += f"\n[{rn}]: {rp.get('text', '')}"
                    except Exception:
                        pass
                ts = float(msg["ts"])
                items.append(RawItem(
                    source    = "slack",
                    item_id   = f"{ch_id}_{msg['ts']}",
                    title     = f"{'DM' if is_im else f'#{ch_name}'}: {text[:80]}",
                    body      = body[:3000],
                    url       = f"https://slack.com/app_redirect?channel={ch_id}&message_ts={msg['ts']}",
                    author    = _username(token, msg.get("user", "unknown"), cache),
                    timestamp = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                    metadata  = {"channel": ch_name, "is_dm": is_im},
                ))
    except Exception as e:
        print(f"[slack] legacy error: {e}")

    print(f"[slack] {len(items)} items (legacy)")
    return items
