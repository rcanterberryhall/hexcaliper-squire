"""
outlook_sidecar.py — Outlook email ingestion sidecar (Windows).

Reads recent emails from the local Outlook client via ``win32com`` and
POSTs them to the Squire ``/ingest`` endpoint.  Intended to run on the
Windows host machine where Outlook is installed; the Squire Docker stack
does not have access to ``win32com`` and relies on this script to supply
email data.

Cloudflare Access service token headers are included on every request so
that the ``/ingest`` endpoint is reachable through the CF Access policy
protecting ``squire.hexcaliper.com``.

Usage::

    pip install requests pywin32
    python outlook_sidecar.py

Schedule with Windows Task Scheduler to run every 30–60 minutes.
"""
import re
import sys
import requests
from datetime import datetime, timedelta

PAGE_API_URL          = "https://squire.hexcaliper.com/page/api"
CF_CLIENT_ID          = "your-cf-client-id"
CF_CLIENT_SECRET      = "your-cf-client-secret"
LOOKBACK_HOURS        = 48
MAX_EMAILS            = 75


def fetch() -> list[dict]:
    """
    Connect to the local Outlook client and fetch recent emails.

    Uses ``win32com.client`` to access the MAPI namespace and retrieve
    messages from the default Inbox.  Messages are filtered to the lookback
    window defined by ``LOOKBACK_HOURS`` and capped at ``MAX_EMAILS``.

    :return: List of normalised email dicts ready for ``POST /ingest``.
    :rtype: list[dict]
    :raises SystemExit: If ``pywin32`` is not installed or Outlook is not running.
    """
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
    """
    POST fetched email items to the Squire ``/ingest`` endpoint.

    Includes Cloudflare Access service token headers so requests pass
    through the CF Access policy protecting ``squire.hexcaliper.com``.
    The API deduplicates by ``item_id`` and queues new items for AI
    analysis in the background.

    :param items: List of email dicts as returned by ``fetch()``.
    :type items: list[dict]
    :raises SystemExit: If the API is unreachable or returns an error.
    """
    if not items:
        print("No emails found in lookback window.")
        return
    try:
        r = requests.post(
            f"{PAGE_API_URL}/ingest",
            json={"items": items},
            headers={
                "CF-Access-Client-Id":     CF_CLIENT_ID,
                "CF-Access-Client-Secret": CF_CLIENT_SECRET,
            },
            timeout=30,
        )
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