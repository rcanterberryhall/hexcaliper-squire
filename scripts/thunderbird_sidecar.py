"""
Hexcaliper Squire — Thunderbird Sidecar (Ubuntu / Linux)
=======================================================
Reads recent emails from Thunderbird's local mbox/Maildir cache on disk
and POSTs them to the Squire API. No API tokens or IT involvement required.

Usage:
    pip install requests
    python thunderbird_sidecar.py

Crontab (every 30 minutes):
    */30 * * * * python3 /path/to/thunderbird_sidecar.py >> /tmp/page-sidecar.log 2>&1

Requirements:
    - Thunderbird installed and configured with your M365 IMAP account
    - Account Settings → Synchronization & Storage →
      "Keep messages for this account on this computer" must be checked
"""
import email
import email.header
import hashlib
import mailbox
import re
import sys
import requests
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
PAGE_API_URL   = "http://localhost:8082/page/api"
LOOKBACK_HOURS = 48
MAX_EMAILS     = 75

# Leave as None to auto-detect, or set explicitly:
# THUNDERBIRD_PROFILE = "/home/youruser/.thunderbird/abc123.default-release"
THUNDERBIRD_PROFILE: str | None = None

# Thunderbird stores IMAP folders by server name.
# "INBOX" works for most setups; try "INBOX.INBOX" if not found.
IMAP_FOLDER = "INBOX"
# ─────────────────────────────────────────────────────────────────────────────


def find_profile() -> Path:
    tb_dir = Path.home() / ".thunderbird"
    if not tb_dir.exists():
        sys.exit("ERROR: ~/.thunderbird not found — is Thunderbird installed?")

    profiles_ini = tb_dir / "profiles.ini"
    if not profiles_ini.exists():
        sys.exit("ERROR: ~/.thunderbird/profiles.ini not found")

    current_path = None
    is_default   = False
    default_path = None

    for line in profiles_ini.read_text().splitlines():
        line = line.strip()
        if line.startswith("Path="):
            current_path = line[5:]
        elif line == "Default=1":
            is_default = True
        elif line == "" and current_path:
            if is_default:
                default_path = current_path
            is_default   = False
            current_path = None

    if not default_path:
        for entry in tb_dir.iterdir():
            if entry.is_dir() and "." in entry.name:
                default_path = entry.name
                break

    if not default_path:
        sys.exit("ERROR: Could not locate a Thunderbird profile directory")

    profile = (
        tb_dir / default_path
        if not Path(default_path).is_absolute()
        else Path(default_path)
    )
    print(f"Profile: {profile}")
    return profile


def find_account_dir(profile: Path) -> Path:
    imap_root = profile / "ImapMail"
    if not imap_root.exists():
        sys.exit("ERROR: No ImapMail directory — is your account configured as IMAP?")

    accounts = sorted([d for d in imap_root.iterdir() if d.is_dir()])
    if not accounts:
        sys.exit("ERROR: No IMAP account directories found")

    if len(accounts) > 1:
        print("Multiple IMAP accounts:")
        for a in accounts:
            print(f"  {a.name}")
        print(f"Using: {accounts[0].name}")

    return accounts[0]


def decode_header_val(val: str) -> str:
    try:
        parts = email.header.decode_header(val or "")
        out   = []
        for part, charset in parts:
            if isinstance(part, bytes):
                out.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(str(part))
        return " ".join(out)
    except Exception:
        return val or ""


def extract_body(msg: email.message.Message) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == "text/plain"
                    and "attachment" not in str(part.get("Content-Disposition", ""))):
                try:
                    charset = part.get_content_charset() or "utf-8"
                    body    = part.get_payload(decode=True).decode(charset, errors="replace")
                    break
                except Exception:
                    continue
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            body    = msg.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            pass
    return re.sub(r'\n{3,}', '\n\n', body).strip()


def parse_date(msg) -> datetime | None:
    date_str = msg.get("Date", "")
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load_messages(account_dir: Path, cutoff: datetime) -> list:
    mbox_file = account_dir / IMAP_FOLDER
    raw = []

    if mbox_file.is_file():
        print(f"Reading mbox: {mbox_file}")
        try:
            mb = mailbox.mbox(str(mbox_file))
            mb.lock()
            try:
                for msg in mb:
                    dt = parse_date(msg)
                    if dt and dt >= cutoff:
                        raw.append(msg)
            finally:
                mb.unlock()
        except Exception as e:
            print(f"  Warning: {e}")

    elif (account_dir / IMAP_FOLDER / "cur").exists():
        print(f"Reading Maildir: {account_dir / IMAP_FOLDER}")
        try:
            md = mailbox.Maildir(str(account_dir / IMAP_FOLDER))
            for msg in md:
                dt = parse_date(msg)
                if dt and dt >= cutoff:
                    raw.append(msg)
        except Exception as e:
            print(f"  Warning: {e}")

    else:
        # Fallback: find any inbox-like mbox file
        candidates = [
            f for f in account_dir.iterdir()
            if f.is_file() and not f.suffix == ".msf"
            and f.name.lower().startswith("inbox")
        ]
        if candidates:
            print(f"Reading mbox (fallback): {candidates[0]}")
            try:
                mb = mailbox.mbox(str(candidates[0]))
                mb.lock()
                try:
                    for msg in mb:
                        dt = parse_date(msg)
                        if dt and dt >= cutoff:
                            raw.append(msg)
                finally:
                    mb.unlock()
            except Exception as e:
                print(f"  Warning: {e}")
        else:
            print(f"ERROR: Could not find inbox at {account_dir}")
            print("Contents:")
            for f in sorted(account_dir.iterdir()):
                print(f"  {f.name}")
            sys.exit(1)

    return raw


def fetch() -> list[dict]:
    profile     = Path(THUNDERBIRD_PROFILE) if THUNDERBIRD_PROFILE else find_profile()
    account_dir = find_account_dir(profile)
    cutoff      = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    raw_msgs    = load_messages(account_dir, cutoff)

    raw_msgs.sort(
        key=lambda m: parse_date(m) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    raw_msgs = raw_msgs[:MAX_EMAILS]

    items = []
    for msg in raw_msgs:
        subject  = decode_header_val(msg.get("Subject", "(no subject)"))
        sender   = decode_header_val(msg.get("From", ""))
        body     = extract_body(msg)
        dt       = parse_date(msg) or datetime.now(timezone.utc)
        msg_id   = msg.get("Message-ID", "").strip("<>")
        item_id  = msg_id if msg_id else hashlib.sha1(
            f"{sender}{subject}{dt.isoformat()}".encode()
        ).hexdigest()[:20]

        items.append({
            "source":    "outlook",          # same label as win32com path
            "item_id":   f"tb_{item_id}",
            "title":     subject,
            "body":      body[:3000],
            "url":       "",
            "author":    sender,
            "timestamp": dt.isoformat(),
            "metadata":  {"via": "thunderbird"},
        })

    return items


def post(items: list[dict]) -> None:
    if not items:
        print("No emails found in lookback window.")
        return
    try:
        r = requests.post(f"{PAGE_API_URL}/ingest", json={"items": items}, timeout=30)
        r.raise_for_status()
        result = r.json()
        print(f"Sent {len(items)} → accepted {result.get('received','?')}, skipped {result.get('skipped','?')}")
    except requests.ConnectionError:
        sys.exit(f"ERROR: Could not reach API at {PAGE_API_URL} — is Docker running?")
    except Exception as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    print(f"Fetching Thunderbird emails (last {LOOKBACK_HOURS}h)...")
    items = fetch()
    print(f"Found {len(items)} emails")
    post(items)
