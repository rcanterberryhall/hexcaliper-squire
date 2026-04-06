"""
test_attention.py — B7: Adaptive attention model.

Tests for attention.py math (centroid update, decay, score normalization)
and the API endpoints (/analyses/{id}/action, /attention/summary).
"""
import math
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import pytest

import attention as attn


# ── Unit tests: helpers ───────────────────────────────────────────────────────

def test_decay_weight_recent():
    now = datetime.now(timezone.utc)
    ts  = now.isoformat()
    assert attn._decay_weight(ts, now) == 1.0


def test_decay_weight_30_days():
    now = datetime.now(timezone.utc)
    ts  = (now - timedelta(days=35)).isoformat()
    assert attn._decay_weight(ts, now) == 0.5


def test_decay_weight_60_days():
    now = datetime.now(timezone.utc)
    ts  = (now - timedelta(days=65)).isoformat()
    assert attn._decay_weight(ts, now) == 0.25


def test_decay_weight_bad_ts():
    now = datetime.now(timezone.utc)
    assert attn._decay_weight("not-a-date", now) == 1.0


def test_cosine_identical():
    v = [1.0, 0.0, 0.0]
    assert abs(attn._cosine(v, v) - 1.0) < 1e-6


def test_cosine_orthogonal():
    assert abs(attn._cosine([1.0, 0.0], [0.0, 1.0])) < 1e-6


def test_cosine_empty():
    assert attn._cosine([], [1.0]) == 0.0


def test_weighted_centroid_unit():
    vecs = [[1.0, 0.0], [0.0, 1.0]]
    ws   = [1.0, 1.0]
    c = attn._weighted_centroid(vecs, ws)
    assert c is not None
    norm = math.sqrt(sum(x*x for x in c))
    assert abs(norm - 1.0) < 1e-5


def test_weighted_centroid_empty():
    assert attn._weighted_centroid([], []) is None


def test_weighted_centroid_zero_weight():
    assert attn._weighted_centroid([[1.0, 0.0]], [0.0]) is None


# ── Unit tests: score / cold start ────────────────────────────────────────────

def test_compute_score_empty_embedding():
    assert attn.compute_score([]) == 0.5


def test_compute_score_cold_start(tmp_path, monkeypatch):
    """Cold start (< 50 actions) returns 0.5 regardless of embedding."""
    import db
    monkeypatch.setattr(db, "count_user_actions", lambda: 10)
    assert attn.compute_score([1.0, 0.0, 0.0]) == 0.5


def test_compute_score_no_centroid(monkeypatch):
    import db
    monkeypatch.setattr(db, "count_user_actions", lambda: 100)
    monkeypatch.setattr(db, "get_model_state", lambda k: {})
    assert attn.compute_score([1.0, 0.0, 0.0]) == 0.5


def test_compute_score_with_centroid(monkeypatch):
    import db
    monkeypatch.setattr(db, "count_user_actions", lambda: 100)
    # attended centroid identical to query → max similarity
    monkeypatch.setattr(db, "get_model_state", lambda k: {
        "attended_centroid": [1.0, 0.0, 0.0],
    })
    score = attn.compute_score([1.0, 0.0, 0.0])
    assert score > 0.5  # above neutral


def test_compute_score_normalized_to_0_1(monkeypatch):
    import db
    monkeypatch.setattr(db, "count_user_actions", lambda: 100)
    monkeypatch.setattr(db, "get_model_state", lambda k: {
        "attended_centroid": [1.0, 0.0, 0.0],
        "ignored_centroid":  [1.0, 0.0, 0.0],
    })
    score = attn.compute_score([1.0, 0.0, 0.0])
    assert 0.0 <= score <= 1.0


# ── Unit tests: cold_start flag ───────────────────────────────────────────────

def test_is_cold_start_true(monkeypatch):
    import db
    monkeypatch.setattr(db, "count_user_actions", lambda: 10)
    assert attn.is_cold_start()


def test_is_cold_start_false(monkeypatch):
    import db
    monkeypatch.setattr(db, "count_user_actions", lambda: 100)
    assert not attn.is_cold_start()


# ── get_summary ───────────────────────────────────────────────────────────────

def test_get_summary_cold_start(monkeypatch):
    import db
    monkeypatch.setattr(db, "count_user_actions", lambda: 5)
    monkeypatch.setattr(db, "get_model_state", lambda k: None)
    s = attn.get_summary()
    assert s["cold_start"] is True
    assert s["cold_start_msg"]
    assert s["action_count"] == 5


def test_get_summary_active(monkeypatch):
    import db
    monkeypatch.setattr(db, "count_user_actions", lambda: 75)
    monkeypatch.setattr(db, "get_model_state", lambda k: {"attended_count": 50, "ignored_count": 10})
    s = attn.get_summary()
    assert s["cold_start"] is False
    assert s["attended_count"] == 50


# ── API endpoint tests ────────────────────────────────────────────────────────

def test_record_action_endpoint(client):
    from app import analyses
    analyses.insert({
        "item_id": "act-1", "source": "jira", "title": "T",
        "author": "a", "timestamp": "2026-04-05T00:00:00+00:00", "url": "",
        "has_action": False, "priority": "medium", "category": "fyi",
        "summary": "S", "urgency": None, "action_items": "[]",
        "processed_at": "2026-04-05T00:00:00+00:00",
    })
    r = client.post("/analyses/act-1/action", json={"action_type": "opened"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_record_action_missing_type(client):
    r = client.post("/analyses/anything/action", json={})
    assert r.status_code == 422


def test_attention_summary_endpoint(client):
    r = client.get("/attention/summary")
    assert r.status_code == 200
    body = r.json()
    assert "cold_start" in body
    assert "action_count" in body
    assert "cold_start_msg" in body
