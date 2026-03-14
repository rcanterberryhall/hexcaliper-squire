"""
outlook_sidecar.py — Outlook email ingestion sidecar (Windows).

Reads recent emails from the local Outlook client via ``win32com`` and
POSTs them to the Squire ``/ingest`` endpoint.  Intended to run on the
Windows host machine where Outlook is installed; the Squire Docker stack
does not have access to ``win32com`` and relies on this script to supply
email data.

Cloudflare Access service token credentials are stored in Windows Credential
Manager via the ``keyring`` library and never written to disk or code.  Run
with ``--setup`` once to store them:

    python outlook_sidecar.py --setup

Then run normally (or via Task Scheduler) to ingest emails:

    python outlook_sidecar.py

Usage::

    pip install requests pywin32 keyring
    python outlook_sidecar.py --setup   # first-time credential setup
    python outlook_sidecar.py           # normal / scheduled run

Schedule with Windows Task Scheduler to run every 30–60 minutes.
"""
import re
import sys
import requests
from datetime import datetime, timedelta

PAGE_API_URL   = "https://squire.hexcaliper.com/page/api"
LOOKBACK_HOURS = 48
MAX_EMAILS     = 75

# Windows Credential Manager service name used by keyring.
_KEYRING_SERVICE = "hexcaliper-squire"
_KEY_CLIENT_ID   = "cf_client_id"
_KEY_CLIENT_SECRET = "cf_client_secret"


def _load_credentials() -> tuple[str, str]:
    """
    Load Cloudflare Access service token credentials from Windows Credential Manager.

    :return: Tuple of ``(client_id, client_secret)``.
    :rtype: tuple[str, str]
    :raises SystemExit: If ``keyring`` is not installed or credentials have not
                        been stored yet (run with ``--setup`` first).
    """
    try:
        import keyring
    except ImportError:
        sys.exit("ERROR: keyring not installed.  Run: pip install keyring")

    client_id     = keyring.get_password(_KEYRING_SERVICE, _KEY_CLIENT_ID)     or ""
    client_secret = keyring.get_password(_KEYRING_SERVICE, _KEY_CLIENT_SECRET) or ""

    if not client_id or not client_secret:
        sys.exit(
            "ERROR: Cloudflare Access credentials not found.\n"
            "Run: python outlook_sidecar.py --setup"
        )
    return client_id, client_secret


def _setup() -> None:
    """
    Interactively store Cloudflare Access credentials in Windows Credential Manager.

    Prompts for the CF Access Client ID and Client Secret, then persists them
    via ``keyring`` so they are never written to code or environment variables.

    :raises SystemExit: If ``keyring`` is not installed.
    """
    try:
        import keyring
    except ImportError:
        sys.exit("ERROR: keyring not installed.  Run: pip install keyring")

    print("Hexcaliper Squire — Credential Setup")
    print("Credentials will be stored in Windows Credential Manager.\n")
    client_id     = input("CF Access Client ID:     ").strip()
    client_secret = input("CF Access Client Secret: ").strip()

    if not client_id or not client_secret:
        sys.exit("ERROR: Both values are required.")

    keyring.set_password(_KEYRING_SERVICE, _KEY_CLIENT_ID,     client_id)
    keyring.set_password(_KEYRING_SERVICE, _KEY_CLIENT_SECRET, client_secret)
    print("\nCredentials saved to Windows Credential Manager.")


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

        cutoff     = datetime.now() - timedelta(hours=LOOKBACK_HOURS)
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
                msg      = messages.Item(index)
                subject  = getattr(msg, "Subject", None)
                received = getattr(msg, "ReceivedTime", None)
                if received is None:
                    continue

                dt = datetime(
                    received.year, received.month, received.day,
                    received.hour, received.minute, received.second
                )

                if dt < cutoff:
                    break

                body         = re.sub(r'\n{3,}', '\n\n', (getattr(msg, "Body", "") or "")).strip()
                sender_name  = getattr(msg, "SenderName", "")  or ""
                sender_email = getattr(msg, "SenderEmailAddress", "") or ""

                items.append({
                    "source":    "outlook",
                    "item_id":   str(getattr(msg, "EntryID", "")),
                    "title":     subject or "(no subject)",
                    "body":      body[:3000],
                    "url":       "",
                    "author":    f"{sender_name} <{sender_email}>".strip(),
                    "timestamp": dt.isoformat(),
                    "metadata":  {"is_read": getattr(msg, "UnRead", True) is False},
                })
            except Exception:
                continue

        return items
    finally:
        pythoncom.CoUninitialize()


def post(items: list[dict], client_id: str, client_secret: str) -> None:
    """
    POST fetched email items to the Squire ``/ingest`` endpoint.

    Includes Cloudflare Access service token headers so requests pass
    through the CF Access policy protecting ``squire.hexcaliper.com``.
    The API deduplicates by ``item_id`` and queues new items for AI
    analysis in the background.

    :param items: List of email dicts as returned by ``fetch()``.
    :type items: list[dict]
    :param client_id: CF Access service token Client ID.
    :type client_id: str
    :param client_secret: CF Access service token Client Secret.
    :type client_secret: str
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
                "CF-Access-Client-Id":     client_id,
                "CF-Access-Client-Secret": client_secret,
            },
            timeout=30,
        )
        r.raise_for_status()
        result = r.json()
        print(f"Sent {len(items)} → accepted {result.get('received','?')}, skipped {result.get('skipped','?')}")
    except requests.ConnectionError:
        sys.exit(f"ERROR: Could not reach API at {PAGE_API_URL} — is the appliance reachable?")
    except Exception as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--setup":
        _setup()
    else:
        cf_id, cf_secret = _load_credentials()
        print(f"Fetching Outlook emails (last {LOOKBACK_HOURS}h)...", flush=True)
        emails = fetch()
        print(f"Found {len(emails)} emails", flush=True)
        post(emails, cf_id, cf_secret)
