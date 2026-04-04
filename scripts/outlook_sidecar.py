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

PAGE_API_URL        = "https://lancellmot.hexcaliper.com/page/api"
LOOKBACK_HOURS      = 48
MAX_EMAILS          = 75
SEED_LOOKBACK_HOURS = 720   # 30 days
SEED_MAX_EMAILS     = 500

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


def _read_recipients(msg) -> tuple[list[str], list[str]]:
    """
    Extract To and CC recipient strings from a COM message object.

    Returns ``(to_list, cc_list)`` as lists of ``"Name <addr>"`` strings.
    Falls back to the plain ``msg.To`` / ``msg.CC`` string properties if the
    Recipients COM collection is inaccessible.

    :return: Tuple of ``(to_list, cc_list)``.
    :rtype: tuple[list[str], list[str]]
    """
    to_list, cc_list = [], []
    try:
        for r in msg.Recipients:
            addr  = getattr(r, "Address", "") or ""
            name  = getattr(r, "Name", "")    or ""
            entry = f"{name} <{addr}>" if name and addr else (name or addr)
            rtype = getattr(r, "Type", 1)
            if rtype == 1:    # olTo
                to_list.append(entry)
            elif rtype == 2:  # olCC
                cc_list.append(entry)
    except Exception:
        to_list = [getattr(msg, "To", "") or ""]
        cc_list = [getattr(msg, "CC", "") or ""]
    return to_list, cc_list


def _normalise_subject(subject: str) -> str:
    """
    Strip Re:/Fwd:/Fw: prefixes from a subject line for use as conversation_topic.

    :param subject: Raw email subject string.
    :return: Cleaned subject with reply/forward prefixes removed.
    """
    return re.sub(r'^(Re|Fwd?|AW|WG):\s*', '', subject or "", flags=re.IGNORECASE).strip()


def _fetch_folder(
    ns,
    folder_id: int,
    cutoff: "datetime",
    max_emails: int,
    direction: str,
    time_field: str,
) -> list[dict]:
    """
    Fetch emails from a single Outlook folder.

    :param ns: MAPI namespace COM object.
    :param folder_id: Outlook folder constant (6 = Inbox, 5 = Sent).
    :param cutoff: Only fetch messages at or after this datetime.
    :param max_emails: Maximum number of messages to return.
    :param direction: ``"received"`` or ``"sent"``.
    :param time_field: COM property name for the message timestamp
                       (``"ReceivedTime"`` for Inbox, ``"SentOn"`` for Sent).
    :return: List of normalised email dicts.
    """
    folder   = ns.GetDefaultFolder(folder_id)
    messages = folder.Items
    messages.Sort(f"[{time_field}]", True)

    cutoff_str = cutoff.strftime("%m/%d/%Y %I:%M %p")
    try:
        messages = messages.Restrict(f"[{time_field}] >= '{cutoff_str}'")
        messages.Sort(f"[{time_field}]", True)
    except Exception:
        pass

    items = []
    count = messages.Count
    for index in range(1, min(count, max_emails) + 1):
        try:
            msg      = messages.Item(index)
            subject  = getattr(msg, "Subject", None)
            ts_com   = getattr(msg, time_field, None)
            if ts_com is None:
                continue

            dt = datetime(
                ts_com.year, ts_com.month, ts_com.day,
                ts_com.hour, ts_com.minute, ts_com.second,
            )
            if dt < cutoff:
                break

            body         = re.sub(r'\n{3,}', '\n\n', (getattr(msg, "Body", "") or "")).strip()
            sender_name  = getattr(msg, "SenderName", "")        or ""
            sender_email = getattr(msg, "SenderEmailAddress", "") or ""
            to_list, cc_list = _read_recipients(msg)

            # Detect reply/forward via LastVerbExecuted (inbox only; sent = 0):
            # 102 = olReplyToSender, 103 = olReplyToAll, 104 = olForward
            last_verb    = getattr(msg, "LastVerbExecuted", 0) or 0
            is_replied   = last_verb in (102, 103)
            is_forwarded = last_verb == 104
            replied_at   = None
            if last_verb in (102, 103, 104):
                try:
                    rv = msg.LastVerbExecutionTime
                    replied_at = datetime(
                        rv.year, rv.month, rv.day,
                        rv.hour, rv.minute, rv.second,
                    ).isoformat()
                except Exception:
                    pass

            conv_id    = getattr(msg, "ConversationID", "")    or ""
            conv_topic = _normalise_subject(
                getattr(msg, "ConversationTopic", "") or subject or ""
            )

            items.append({
                "source":    "outlook",
                "item_id":   str(getattr(msg, "EntryID", "")),
                "title":     subject or "(no subject)",
                "body":      body[:3000],
                "url":       "",
                "author":    f"{sender_name} <{sender_email}>".strip(),
                "timestamp": dt.isoformat(),
                "metadata":  {
                    "direction":          direction,
                    "is_read":            getattr(msg, "UnRead", True) is False,
                    "to":                 "; ".join(to_list),
                    "cc":                 "; ".join(cc_list),
                    "is_replied":         is_replied,
                    "is_forwarded":       is_forwarded,
                    "replied_at":         replied_at,
                    "conversation_id":    conv_id,
                    "conversation_topic": conv_topic,
                },
            })
        except Exception:
            continue

    return items


def fetch(lookback_hours: int = LOOKBACK_HOURS, max_emails: int = MAX_EMAILS) -> list[dict]:
    """
    Connect to the local Outlook client and fetch recent emails from both
    Inbox and Sent Items.

    Uses ``win32com.client`` to access the MAPI namespace.  Each message is
    enriched with ``conversation_id`` and ``conversation_topic`` for
    graph-context linkage, and a ``direction`` flag (``"received"`` /
    ``"sent"``) so the LLM can apply appropriate hierarchy rules.

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
        except Exception as e:
            sys.exit(f"ERROR: Could not connect to Outlook — is it running? ({e})")

        cutoff = datetime.now() - timedelta(hours=lookback_hours)

        print("Fetching Inbox...", flush=True)
        inbox_items = _fetch_folder(
            ns, folder_id=6, cutoff=cutoff, max_emails=max_emails,
            direction="received", time_field="ReceivedTime",
        )
        print(f"  Inbox: {len(inbox_items)} items", flush=True)

        print("Fetching Sent Items...", flush=True)
        sent_items = _fetch_folder(
            ns, folder_id=5, cutoff=cutoff, max_emails=max_emails,
            direction="sent", time_field="SentOn",
        )
        print(f"  Sent:  {len(sent_items)} items", flush=True)

        return inbox_items + sent_items
    finally:
        pythoncom.CoUninitialize()


_POST_BATCH = 50   # items per /ingest request — keeps payloads well under nginx limits


def post(items: list[dict], client_id: str, client_secret: str) -> None:
    """
    POST fetched email items to the Squire ``/ingest`` endpoint in batches.

    Large seed runs (500 emails) would exceed nginx's default body-size limit
    in a single request.  Items are chunked into batches of ``_POST_BATCH``
    and POSTed sequentially; the API deduplicates by ``item_id`` so retries
    are safe.

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

    headers = {
        "CF-Access-Client-Id":     client_id,
        "CF-Access-Client-Secret": client_secret,
    }
    total_received = total_skipped = 0
    batches = [items[i:i + _POST_BATCH] for i in range(0, len(items), _POST_BATCH)]
    for idx, batch in enumerate(batches, 1):
        try:
            r = requests.post(
                f"{PAGE_API_URL}/ingest",
                json={"items": batch},
                headers=headers,
                timeout=30,
            )
            r.raise_for_status()
            result = r.json()
            total_received += result.get("received", 0)
            total_skipped  += result.get("skipped",  0)
            print(f"  Batch {idx}/{len(batches)}: accepted {result.get('received','?')}, "
                  f"skipped {result.get('skipped','?')}", flush=True)
        except requests.ConnectionError:
            sys.exit(f"ERROR: Could not reach API at {PAGE_API_URL} — is the appliance reachable?")
        except Exception as e:
            sys.exit(f"ERROR: {e}")

    print(f"Done — {len(items)} sent, {total_received} accepted, {total_skipped} skipped.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--setup":
        _setup()
    elif len(sys.argv) > 1 and sys.argv[1] == "--seed":
        cf_id, cf_secret = _load_credentials()
        print(f"SEED MODE — fetching Outlook emails (last {SEED_LOOKBACK_HOURS}h / {SEED_LOOKBACK_HOURS//24} days, cap {SEED_MAX_EMAILS})...", flush=True)
        emails = fetch(lookback_hours=SEED_LOOKBACK_HOURS, max_emails=SEED_MAX_EMAILS)
        print(f"Found {len(emails)} emails", flush=True)
        post(emails, cf_id, cf_secret)
    else:
        cf_id, cf_secret = _load_credentials()
        print(f"Fetching Outlook emails (last {LOOKBACK_HOURS}h)...", flush=True)
        emails = fetch()
        print(f"Found {len(emails)} emails", flush=True)
        post(emails, cf_id, cf_secret)
