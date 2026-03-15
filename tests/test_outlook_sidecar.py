"""
test_outlook_sidecar.py — Tests for the Outlook sidecar script.

Covers credential loading, email fetching (win32com mocked), and the
POST /ingest call.  All platform-specific imports (win32com, pythoncom,
keyring) are mocked so the suite runs on any OS.
"""
import sys
import types
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch, call


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_msg(
    subject="Test Subject",
    entry_id="ENTRY001",
    body="Hello world",
    sender_name="Alice Smith",
    sender_email="alice@example.com",
    unread=True,
    received: datetime = None,
):
    """Build a mock Outlook message COM object."""
    if received is None:
        received = datetime(2026, 3, 14, 10, 0, 0)

    received_mock       = MagicMock()
    received_mock.year  = received.year
    received_mock.month = received.month
    received_mock.day   = received.day
    received_mock.hour  = received.hour
    received_mock.minute = received.minute
    received_mock.second = received.second

    msg = MagicMock()
    msg.Subject            = subject
    msg.ReceivedTime       = received_mock
    msg.Body               = body
    msg.SenderName         = sender_name
    msg.SenderEmailAddress = sender_email
    msg.EntryID            = entry_id
    msg.UnRead             = unread
    return msg


def _make_win32_stack(messages: list):
    """
    Build a mock win32com + pythoncom environment for a given message list.

    Returns ``(mock_pythoncom, mock_win32com)`` ready to be injected via
    ``sys.modules``.  ``Restrict()`` is wired to return the same mock so
    that ``Count`` and ``Item`` remain intact after the time filter is applied.
    """
    mock_messages          = MagicMock()
    mock_messages.Count    = len(messages)
    mock_messages.Item     = lambda i: messages[i - 1]
    # Restrict() returns the same mock so Count/Item stay valid after filtering.
    mock_messages.Restrict = MagicMock(return_value=mock_messages)

    mock_inbox       = MagicMock()
    mock_inbox.Items = mock_messages

    mock_ns = MagicMock()
    mock_ns.GetDefaultFolder.return_value = mock_inbox

    mock_app = MagicMock()
    mock_app.GetNamespace.return_value = mock_ns

    mock_dispatch = MagicMock(return_value=mock_app)

    mock_win32com                 = types.ModuleType("win32com")
    mock_win32com.client          = MagicMock()
    mock_win32com.client.Dispatch = mock_dispatch

    mock_pythoncom                = types.ModuleType("pythoncom")
    mock_pythoncom.CoInitialize   = MagicMock()
    mock_pythoncom.CoUninitialize = MagicMock()

    return mock_pythoncom, mock_win32com


def _sidecar():
    """Import outlook_sidecar with a clean module cache each time."""
    if "outlook_sidecar" in sys.modules:
        del sys.modules["outlook_sidecar"]
    import outlook_sidecar
    return outlook_sidecar


# ── Credential loading ────────────────────────────────────────────────────────

def test_load_credentials_returns_stored_values():
    mock_keyring = MagicMock()
    mock_keyring.get_password.side_effect = lambda svc, key: (
        "test-client-id" if key == "cf_client_id" else "test-client-secret"
    )
    with patch.dict(sys.modules, {"keyring": mock_keyring}):
        sidecar = _sidecar()
        cid, csec = sidecar._load_credentials()
    assert cid  == "test-client-id"
    assert csec == "test-client-secret"


def test_load_credentials_exits_when_missing(capsys):
    mock_keyring = MagicMock()
    mock_keyring.get_password.return_value = None
    with patch.dict(sys.modules, {"keyring": mock_keyring}):
        sidecar = _sidecar()
        with pytest.raises(SystemExit):
            sidecar._load_credentials()


def test_load_credentials_exits_when_keyring_not_installed():
    with patch.dict(sys.modules, {"keyring": None}):
        sidecar = _sidecar()
        with pytest.raises(SystemExit):
            sidecar._load_credentials()


# ── fetch() ───────────────────────────────────────────────────────────────────

def test_fetch_returns_normalised_items():
    msg = _make_msg(
        subject      = "Budget Review",
        entry_id     = "ENTRY123",
        body         = "Please review by Friday.",
        sender_name  = "Bob Jones",
        sender_email = "bob@example.com",
        unread       = True,
        received     = datetime(2026, 3, 14, 9, 0, 0),
    )
    mock_pythoncom, mock_win32com = _make_win32_stack([msg])
    with patch.dict(sys.modules, {"pythoncom": mock_pythoncom, "win32com": mock_win32com, "win32com.client": mock_win32com.client}):
        sidecar = _sidecar()
        items = sidecar.fetch()

    assert len(items) == 1
    item = items[0]
    assert item["source"]   == "outlook"
    assert item["item_id"]  == "ENTRY123"
    assert item["title"]    == "Budget Review"
    assert "Bob Jones"      in item["author"]
    assert "bob@example.com" in item["author"]
    assert item["body"]     == "Please review by Friday."
    assert item["metadata"]["is_read"] is False  # UnRead=True means the email has NOT been read


def test_fetch_uses_no_subject_fallback():
    msg = _make_msg(subject=None, entry_id="E2")
    mock_pythoncom, mock_win32com = _make_win32_stack([msg])
    with patch.dict(sys.modules, {"pythoncom": mock_pythoncom, "win32com": mock_win32com, "win32com.client": mock_win32com.client}):
        sidecar = _sidecar()
        items = sidecar.fetch()
    assert items[0]["title"] == "(no subject)"


def test_fetch_truncates_body_to_3000_chars():
    msg = _make_msg(body="x" * 5000, entry_id="E3")
    mock_pythoncom, mock_win32com = _make_win32_stack([msg])
    with patch.dict(sys.modules, {"pythoncom": mock_pythoncom, "win32com": mock_win32com, "win32com.client": mock_win32com.client}):
        sidecar = _sidecar()
        items = sidecar.fetch()
    assert len(items[0]["body"]) == 3000


def test_fetch_skips_messages_without_received_time():
    msg = _make_msg(entry_id="E4")
    msg.ReceivedTime = None
    mock_pythoncom, mock_win32com = _make_win32_stack([msg])
    with patch.dict(sys.modules, {"pythoncom": mock_pythoncom, "win32com": mock_win32com, "win32com.client": mock_win32com.client}):
        sidecar = _sidecar()
        items = sidecar.fetch()
    assert items == []


def test_fetch_exits_when_pywin32_not_installed():
    with patch.dict(sys.modules, {"pythoncom": None, "win32com": None}):
        sidecar = _sidecar()
        with pytest.raises(SystemExit):
            sidecar.fetch()


def test_fetch_exits_when_outlook_not_running():
    mock_pythoncom, mock_win32com = _make_win32_stack([])
    mock_win32com.client.Dispatch.side_effect = Exception("Outlook not running")
    with patch.dict(sys.modules, {"pythoncom": mock_pythoncom, "win32com": mock_win32com, "win32com.client": mock_win32com.client}):
        sidecar = _sidecar()
        with pytest.raises(SystemExit):
            sidecar.fetch()


def test_fetch_collapses_excessive_blank_lines():
    msg = _make_msg(body="line1\n\n\n\n\nline2", entry_id="E5")
    mock_pythoncom, mock_win32com = _make_win32_stack([msg])
    with patch.dict(sys.modules, {"pythoncom": mock_pythoncom, "win32com": mock_win32com, "win32com.client": mock_win32com.client}):
        sidecar = _sidecar()
        items = sidecar.fetch()
    assert "\n\n\n" not in items[0]["body"]


# ── post() ────────────────────────────────────────────────────────────────────

def test_post_sends_cf_access_headers():
    sidecar = _sidecar()
    items   = [{"item_id": "E1", "source": "outlook", "title": "Hi"}]

    mock_resp = MagicMock()
    mock_resp.ok   = True
    mock_resp.json.return_value = {"received": 1, "skipped": 0}
    mock_resp.raise_for_status.return_value = None

    with patch("outlook_sidecar.requests.post", return_value=mock_resp) as mock_post:
        sidecar.post(items, "my-client-id", "my-client-secret")

    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["CF-Access-Client-Id"]     == "my-client-id"
    assert kwargs["headers"]["CF-Access-Client-Secret"] == "my-client-secret"


def test_post_sends_items_as_json():
    sidecar = _sidecar()
    items   = [{"item_id": "E1", "source": "outlook", "title": "Hi"}]

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"received": 1, "skipped": 0}

    with patch("outlook_sidecar.requests.post", return_value=mock_resp) as mock_post:
        sidecar.post(items, "cid", "csec")

    _, kwargs = mock_post.call_args
    assert kwargs["json"] == {"items": items}


def test_post_does_nothing_for_empty_list(capsys):
    sidecar = _sidecar()
    with patch("outlook_sidecar.requests.post") as mock_post:
        sidecar.post([], "cid", "csec")
    mock_post.assert_not_called()
    assert "No emails" in capsys.readouterr().out


def test_post_exits_on_connection_error():
    sidecar = _sidecar()
    import requests as req
    with patch("outlook_sidecar.requests.post", side_effect=req.ConnectionError):
        with pytest.raises(SystemExit):
            sidecar.post([{"item_id": "E1"}], "cid", "csec")


def test_post_prints_received_and_skipped_counts(capsys):
    sidecar = _sidecar()
    items   = [{"item_id": "E1"}, {"item_id": "E2"}]

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"received": 1, "skipped": 1}

    with patch("outlook_sidecar.requests.post", return_value=mock_resp):
        sidecar.post(items, "cid", "csec")

    out = capsys.readouterr().out
    assert "accepted 1" in out
    assert "skipped 1"  in out
