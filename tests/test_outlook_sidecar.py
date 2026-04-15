"""
test_outlook_sidecar.py — Tests for the Outlook sidecar script.

Covers credential loading, email fetching (win32com mocked), and the
POST /ingest call.  All platform-specific imports (win32com, pythoncom,
keyring) are mocked so the suite runs on any OS.
"""
import sys
import types
import pytest
from datetime import datetime, timedelta
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
    sent_on: datetime = None,
    conversation_id="CONV001",
    conversation_topic="Test Subject",
):
    """Build a mock Outlook message COM object."""
    ts = received or sent_on or (datetime.now() - timedelta(hours=1))

    def _com_dt(dt):
        m = MagicMock()
        m.year, m.month, m.day = dt.year, dt.month, dt.day
        m.hour, m.minute, m.second = dt.hour, dt.minute, dt.second
        return m

    msg = MagicMock()
    msg.Subject            = subject
    msg.ReceivedTime       = _com_dt(ts)
    msg.SentOn             = _com_dt(ts)
    msg.Body               = body
    msg.SenderName         = sender_name
    msg.SenderEmailAddress = sender_email
    msg.EntryID            = entry_id
    msg.UnRead             = unread
    msg.ConversationID     = conversation_id
    msg.ConversationTopic  = conversation_topic
    return msg


def _make_folder_mock(messages: list) -> MagicMock:
    """Build a mock Outlook folder with the given message list."""
    mock_messages          = MagicMock()
    mock_messages.Count    = len(messages)
    mock_messages.Item     = lambda i: messages[i - 1]
    mock_messages.Restrict = MagicMock(return_value=mock_messages)
    mock_folder       = MagicMock()
    mock_folder.Items = mock_messages
    return mock_folder


def _make_win32_stack(inbox_messages: list, sent_messages: list = None):
    """
    Build a mock win32com + pythoncom environment.

    ``GetDefaultFolder(6)`` returns a folder with ``inbox_messages``.
    ``GetDefaultFolder(5)`` returns a folder with ``sent_messages`` (empty by default).

    Returns ``(mock_pythoncom, mock_win32com)`` ready for ``sys.modules``.
    """
    sent_messages = sent_messages or []

    inbox_folder = _make_folder_mock(inbox_messages)
    sent_folder  = _make_folder_mock(sent_messages)

    mock_ns = MagicMock()
    mock_ns.GetDefaultFolder.side_effect = lambda fid: (
        inbox_folder if fid == 6 else sent_folder
    )

    mock_app = MagicMock()
    mock_app.GetNamespace.return_value = mock_ns

    mock_win32com                 = types.ModuleType("win32com")
    mock_win32com.client          = MagicMock()
    mock_win32com.client.Dispatch = MagicMock(return_value=mock_app)

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
        received     = datetime.now() - timedelta(hours=1),
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


def test_fetch_includes_conversation_fields():
    msg = _make_msg(
        entry_id="CONV_TEST",
        subject="Re: Budget Review",
        conversation_id="CONV-ID-123",
        conversation_topic="Budget Review",
    )
    mock_pythoncom, mock_win32com = _make_win32_stack([msg])
    with patch.dict(sys.modules, {"pythoncom": mock_pythoncom, "win32com": mock_win32com, "win32com.client": mock_win32com.client}):
        sidecar = _sidecar()
        items = sidecar.fetch()
    assert len(items) == 1
    meta = items[0]["metadata"]
    assert meta["conversation_id"]    == "CONV-ID-123"
    assert meta["conversation_topic"] == "Budget Review"
    assert meta["direction"]          == "received"


def test_fetch_sent_folder_sets_direction():
    sent_msg = _make_msg(
        entry_id="SENT001",
        subject="My sent email",
        conversation_id="CONV-SENT",
        conversation_topic="My sent email",
    )
    mock_pythoncom, mock_win32com = _make_win32_stack([], sent_messages=[sent_msg])
    with patch.dict(sys.modules, {"pythoncom": mock_pythoncom, "win32com": mock_win32com, "win32com.client": mock_win32com.client}):
        sidecar = _sidecar()
        items = sidecar.fetch()
    assert len(items) == 1
    assert items[0]["item_id"]              == "SENT001"
    assert items[0]["metadata"]["direction"] == "sent"


def test_fetch_returns_inbox_and_sent_combined():
    inbox_msg = _make_msg(entry_id="IN001", subject="Incoming")
    sent_msg  = _make_msg(entry_id="SN001", subject="Outgoing")
    mock_pythoncom, mock_win32com = _make_win32_stack([inbox_msg], sent_messages=[sent_msg])
    with patch.dict(sys.modules, {"pythoncom": mock_pythoncom, "win32com": mock_win32com, "win32com.client": mock_win32com.client}):
        sidecar = _sidecar()
        items = sidecar.fetch()
    assert len(items) == 2
    directions = {i["metadata"]["direction"] for i in items}
    assert directions == {"received", "sent"}


def test_normalise_subject_strips_re_prefix():
    if "outlook_sidecar" in sys.modules:
        del sys.modules["outlook_sidecar"]
    import outlook_sidecar
    assert outlook_sidecar._normalise_subject("Re: Budget Review") == "Budget Review"
    assert outlook_sidecar._normalise_subject("Fwd: Notes") == "Notes"
    assert outlook_sidecar._normalise_subject("No prefix") == "No prefix"


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


def test_post_exits_with_credential_message_on_401():
    """parsival#57: bad CF credentials surface a clear, actionable message
    (not the opaque `401 Client Error` from requests)."""
    import requests as req
    sidecar = _sidecar()
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.raise_for_status.side_effect = req.HTTPError(
        "401 Client Error: Unauthorized", response=mock_resp
    )
    with patch("outlook_sidecar.requests.post", return_value=mock_resp):
        with pytest.raises(SystemExit) as exc_info:
            sidecar.post([{"item_id": "E1"}], "bad-id", "bad-secret")

    msg = str(exc_info.value)
    assert "Cloudflare Access" in msg
    assert "--setup" in msg


def test_post_exits_with_credential_message_on_403():
    import requests as req
    sidecar = _sidecar()
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.raise_for_status.side_effect = req.HTTPError(
        "403 Client Error: Forbidden", response=mock_resp
    )
    with patch("outlook_sidecar.requests.post", return_value=mock_resp):
        with pytest.raises(SystemExit) as exc_info:
            sidecar.post([{"item_id": "E1"}], "cid", "csec")

    assert "Cloudflare Access" in str(exc_info.value)


def test_post_exits_with_plain_http_message_on_500():
    """Non-auth HTTP errors still exit with a useful message (not swallowed)."""
    import requests as req
    sidecar = _sidecar()
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.raise_for_status.side_effect = req.HTTPError(
        "500 Server Error", response=mock_resp
    )
    with patch("outlook_sidecar.requests.post", return_value=mock_resp):
        with pytest.raises(SystemExit) as exc_info:
            sidecar.post([{"item_id": "E1"}], "cid", "csec")

    msg = str(exc_info.value)
    assert "500" in msg
    assert "Cloudflare Access" not in msg


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


# ── _seed_and_infer() ─────────────────────────────────────────────────────────

def _mock_post_resp(json_body: dict) -> MagicMock:
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = json_body
    return r


def _mock_get_resp(json_body: dict) -> MagicMock:
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = json_body
    return r


def test_seed_and_infer_reaches_review_and_prints_ui_url(capsys):
    """Happy path: ingest completes, seed SM reaches review, UI URL is printed."""
    mock_keyring = MagicMock()
    mock_keyring.get_password.side_effect = lambda svc, key: (
        "test-id" if key == "cf_client_id" else "test-secret"
    )
    mock_pythoncom, mock_win32com = _make_win32_stack([_make_msg()])

    ingest_resp = _mock_post_resp({"received": 1, "skipped": 0})
    seed_start_resp = _mock_post_resp({"state": "waiting_for_ingest"})
    poll_analyzing  = _mock_get_resp({"state": "analyzing",  "progress": "Analysing…"})
    poll_review     = _mock_get_resp({"state": "review",     "progress": "Analysis complete."})

    with patch.dict(sys.modules, {
        "keyring": mock_keyring,
        "pythoncom": mock_pythoncom,
        "win32com": mock_win32com,
        "win32com.client": mock_win32com.client,
    }):
        sidecar = _sidecar()
        with patch("outlook_sidecar.requests.post", side_effect=[ingest_resp, seed_start_resp]), \
             patch("outlook_sidecar.requests.get",  side_effect=[poll_analyzing, poll_review]), \
             patch("outlook_sidecar.time.sleep"):
            sidecar._seed_and_infer()

    out = capsys.readouterr().out
    assert "review"   in out.lower()
    assert "/page/"   in out


def test_seed_and_infer_exits_on_error_state(capsys):
    """If the seed SM reaches error state, _seed_and_infer exits with an error."""
    mock_keyring = MagicMock()
    mock_keyring.get_password.side_effect = lambda svc, key: (
        "test-id" if key == "cf_client_id" else "test-secret"
    )
    mock_pythoncom, mock_win32com = _make_win32_stack([_make_msg()])

    ingest_resp     = _mock_post_resp({"received": 1, "skipped": 0})
    seed_start_resp = _mock_post_resp({"state": "waiting_for_ingest"})
    poll_error      = _mock_get_resp({"state": "error", "progress": "All map batches failed"})

    with patch.dict(sys.modules, {
        "keyring": mock_keyring,
        "pythoncom": mock_pythoncom,
        "win32com": mock_win32com,
        "win32com.client": mock_win32com.client,
    }):
        sidecar = _sidecar()
        with patch("outlook_sidecar.requests.post", side_effect=[ingest_resp, seed_start_resp]), \
             patch("outlook_sidecar.requests.get",  return_value=poll_error), \
             patch("outlook_sidecar.time.sleep"), \
             pytest.raises(SystemExit):
            sidecar._seed_and_infer()


def test_seed_and_infer_exits_when_api_unreachable():
    """If POST /seed is unreachable, exits cleanly."""
    import requests as req
    mock_keyring = MagicMock()
    mock_keyring.get_password.side_effect = lambda svc, key: (
        "test-id" if key == "cf_client_id" else "test-secret"
    )
    mock_pythoncom, mock_win32com = _make_win32_stack([_make_msg()])

    ingest_resp = _mock_post_resp({"received": 1, "skipped": 0})

    with patch.dict(sys.modules, {
        "keyring": mock_keyring,
        "pythoncom": mock_pythoncom,
        "win32com": mock_win32com,
        "win32com.client": mock_win32com.client,
    }):
        sidecar = _sidecar()
        # First call (POST /ingest) succeeds; second (POST /seed) raises ConnectionError
        with patch("outlook_sidecar.requests.post",
                   side_effect=[ingest_resp, req.ConnectionError]), \
             pytest.raises(SystemExit):
            sidecar._seed_and_infer()


def test_seed_and_infer_tolerates_poll_error_and_retries(capsys):
    """A transient GET /seed/status error is printed but polling continues."""
    import requests as req
    mock_keyring = MagicMock()
    mock_keyring.get_password.side_effect = lambda svc, key: (
        "test-id" if key == "cf_client_id" else "test-secret"
    )
    mock_pythoncom, mock_win32com = _make_win32_stack([_make_msg()])

    ingest_resp     = _mock_post_resp({"received": 1, "skipped": 0})
    seed_start_resp = _mock_post_resp({"state": "waiting_for_ingest"})
    poll_review     = _mock_get_resp({"state": "review", "progress": "Done."})

    with patch.dict(sys.modules, {
        "keyring": mock_keyring,
        "pythoncom": mock_pythoncom,
        "win32com": mock_win32com,
        "win32com.client": mock_win32com.client,
    }):
        sidecar = _sidecar()
        with patch("outlook_sidecar.requests.post", side_effect=[ingest_resp, seed_start_resp]), \
             patch("outlook_sidecar.requests.get",
                   side_effect=[req.ConnectionError("timeout"), poll_review]), \
             patch("outlook_sidecar.time.sleep"):
            sidecar._seed_and_infer()

    out = capsys.readouterr().out
    assert "poll error" in out
    assert "/page/"     in out
