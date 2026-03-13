"""
Hexcaliper Squire — Outlook Sidecar (Windows)
============================================
Reads recent emails from the local Outlook client via win32com and
POSTs them to the Squire API. Run on the Windows host alongside the
Docker stack.

Usage:
    pip install requests pywin32
    python outlook_sidecar.py

Schedule with Windows Task Scheduler to run every 30–60 minutes.
"""
import re
import sys
import requests
from datetime import datetime, timedelta

PAGE_API_URL   = "http://localhost:8082/page/api"
LOOKBACK_HOURS = 48
MAX_EMAILS     = 75


def fetch() -> list[dict]:
    try:
        import win32com.client
    except ImportError:
        sys.exit("ERROR: pywin32 not installed.  Run: pip install pywin32")

    try:
        ns       = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        messages = ns.GetDefaultFolder(6).Items  # 6 = olFolderInbox
        messages.Sort("[ReceivedTime]", True)
    except Exception as e:
        sys.exit(f"ERROR: Could not connect to Outlook — is it running? ({e})")

    cutoff = datetime.now() - timedelta(hours=LOOKBACK_HOURS)
    items  = []

    for i, msg in enumerate(messages):
        if i >= MAX_EMAILS:
            break
        try:
            r  = msg.ReceivedTime
            dt = datetime(r.year, r.month, r.day, r.hour, r.minute, r.second)
            if dt < cutoff:
                break
            body = re.sub(r'\n{3,}', '\n\n', (msg.Body or "")).strip()
            items.append({
                "source":    "outlook",
                "item_id":   str(msg.EntryID),
                "title":     msg.Subject or "(no subject)",
                "body":      body[:3000],
                "url":       "",
                "author":    f"{msg.SenderName} <{msg.SenderEmailAddress}>",
                "timestamp": dt.isoformat(),
                "metadata":  {"is_read": msg.UnRead is False},
            })
        except Exception:
            continue

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
    print(f"Fetching Outlook emails (last {LOOKBACK_HOURS}h)...")
    emails = fetch()
    print(f"Found {len(emails)} emails")
    post(emails)
