"""
Outlook connector stub.
Inside Docker this always returns [] — win32com is not available on Linux.
Emails are fed into the API via the sidecar scripts in /scripts:
  Windows  → scripts/outlook_sidecar.py   (win32com)
  Ubuntu   → scripts/thunderbird_sidecar.py  (local mbox/Maildir)
"""
from models import RawItem


def fetch() -> list[RawItem]:
    print("[outlook] running in Docker — use host sidecar to ingest emails")
    return []
