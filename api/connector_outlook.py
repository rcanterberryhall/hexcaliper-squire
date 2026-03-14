"""
connector_outlook.py — Outlook connector stub.

Inside Docker this module is intentionally a no-op — ``win32com`` is not
available on Linux.  Emails are fed into the API via the host sidecar
scripts in ``/scripts``:

- **Windows** → ``scripts/outlook_sidecar.py`` (win32com)
- **Linux/Ubuntu** → ``scripts/thunderbird_sidecar.py`` (local mbox/Maildir)

Both sidecars POST directly to the ``/ingest`` endpoint.
"""
from models import RawItem


def fetch() -> list[RawItem]:
    """
    Return an empty list — Outlook ingestion is handled by the host sidecar.

    :return: Empty list.
    :rtype: list[RawItem]
    """
    print("[outlook] running in Docker — use host sidecar to ingest emails")
    return []
