import os
import sys
import tempfile

# Add api/ and scripts/ to path before any app imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# Point TinyDB at a temp file before app.py is imported (it opens DB at module level)
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

import pytest
from fastapi.testclient import TestClient
import app as _app
from app import app, analyses, todos, scan_logs, settings_tbl, situations_tbl, intel_tbl, embeddings_tbl
import config


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_db():
    """Wipe all tables and reset mutable config/job state before each test."""
    analyses.truncate()
    todos.truncate()
    scan_logs.truncate()
    settings_tbl.truncate()
    situations_tbl.truncate()
    intel_tbl.truncate()
    embeddings_tbl.truncate()
    config.PROJECTS = []
    config.FOCUS_TOPICS = []
    config.NOISE_KEYWORDS = []
    _app._seed_job = {"status": "idle"}
    yield
