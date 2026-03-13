import os
import sys
import tempfile

# Add api/ to path before any app imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

# Point TinyDB at a temp file before app.py is imported (it opens DB at module level)
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

import pytest
from fastapi.testclient import TestClient
from app import app, analyses, todos, scan_logs


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_db():
    """Wipe all tables before each test for isolation."""
    analyses.truncate()
    todos.truncate()
    scan_logs.truncate()
    yield
