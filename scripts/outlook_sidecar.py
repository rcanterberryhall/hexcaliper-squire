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
        import pythoncom
        import win32com.client
    except ImportError:
        sys.exit("ERROR: pywin32 not installed.  Run: pip install pywin32")

    pythoncom.CoInitialize()
    try:
        try:
            print("Connecting to Outlook...", flush=True)
            ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
            print("Opening Inbox...", flush=True)
            inbox = ns.GetDefaultFolder(6)  # 6 = olFolderInbox
            messages = inbox.Items
            print("Sorting messages...", flush=True)
            messages.Sort("[ReceivedTime]", True)
        except Exception as e:
            sys.exit(f"ERROR: Could not connect to Outlook — is it running? ({e})")

        cutoff = datetime.now() - timedelta(hours=LOOKBACK_HOURS)
        cutoff_str = cutoff.strftime("%m/%d/%Y %I:%M %p")

        try:
            print("Applying time filter...", flush=True)
            messages = messages.Restrict(f"[ReceivedTime] >= '{cutoff_str}'")
            messages.Sort("[ReceivedTime]", True)
        except Exception:
            pass

        items = []
        count = messages.Count
        print(f"Filtered item count: {count}", flush=True)

        for index in range(1, min(count, MAX_EMAILS) + 1):
            try:
                msg = messages.Item(index)

                subject = getattr(msg, "Subject", None)
                received = getattr(msg, "ReceivedTime", None)
                if received is None:
                    continue

                dt = datetime(
                    received.year, received.month, received.day,
                    received.hour, received.minute, received.second
                )

                if dt < cutoff:
                    break

                body = re.sub(r'\n{3,}', '\n\n', (getattr(msg, "Body", "") or "")).strip()
                # body = ""

                sender_name = getattr(msg, "SenderName", "") or ""
                sender_email = getattr(msg, "SenderEmailAddress", "") or ""

                items.append({
                    "source": "outlook",
                    "item_id": str(getattr(msg, "EntryID", "")),
                    "title": subject or "(no subject)",
                    "body": body[:3000],
                    "url": "",
                    "author": f"{sender_name} <{sender_email}>".strip(),
                    "timestamp": dt.isoformat(),
                    "metadata": {"is_read": getattr(msg, "UnRead", True) is False},
                })
            except Exception:
                continue

        return items
    finally:
        pythoncom.CoUninitialize()
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
    print(f"Fetching Outlook emails (last {LOOKBACK_HOURS}h)...", flush=True)
    emails = fetch()
    print(f"Found {len(emails)} emails", flush=True)
    post(emails)