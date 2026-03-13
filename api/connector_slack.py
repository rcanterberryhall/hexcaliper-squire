import requests
from datetime import datetime, timedelta
from models import RawItem
import config

BASE = "https://slack.com/api"


def _h() -> dict:
    return {"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"}


def _get(endpoint: str, params: dict = None) -> dict:
    r = requests.get(f"{BASE}/{endpoint}", headers=_h(), params=params or {}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack error: {data.get('error')}")
    return data


def _username(uid: str, cache: dict) -> str:
    if uid in cache:
        return cache[uid]
    try:
        name = _get("users.info", {"user": uid})["user"].get("real_name") or uid
    except Exception:
        name = uid
    cache[uid] = name
    return name


def fetch() -> list[RawItem]:
    if not config.SLACK_BOT_TOKEN or config.SLACK_BOT_TOKEN.startswith("xoxb-your"):
        print("[slack] not configured — skipping")
        return []

    try:
        bot_uid   = _get("auth.test").get("user_id", "")
        cutoff_ts = str((datetime.now() - timedelta(hours=config.LOOKBACK_HOURS)).timestamp())
        cache: dict   = {}
        items: list[RawItem] = []

        channels = _get("conversations.list", {
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
                msgs = _get("conversations.history", {
                    "channel": ch_id,
                    "oldest":  cutoff_ts,
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
                        replies = _get("conversations.replies", {"channel": ch_id, "ts": msg["ts"]})
                        for r in replies.get("messages", [])[1:5]:
                            rn   = _username(r.get("user", "?"), cache)
                            body += f"\n[{rn}]: {r.get('text', '')}"
                    except Exception:
                        pass

                items.append(RawItem(
                    source    = "slack",
                    item_id   = f"{ch_id}_{msg['ts']}",
                    title     = f"{'DM' if is_im else f'#{ch_name}'}: {text[:80]}",
                    body      = body[:3000],
                    url       = f"https://slack.com/app_redirect?channel={ch_id}&message_ts={msg['ts']}",
                    author    = _username(msg.get("user", "unknown"), cache),
                    timestamp = datetime.fromtimestamp(float(msg["ts"])).isoformat(),
                    metadata  = {"channel": ch_name, "is_dm": is_im},
                ))

        print(f"[slack] {len(items)} actionable messages")
        return items

    except Exception as e:
        print(f"[slack] error: {e}")
        return []
