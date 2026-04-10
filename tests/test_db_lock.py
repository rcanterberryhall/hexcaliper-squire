"""Regression tests for the ``db.lock`` re-entrancy contract.

The lock used to be a non-reentrant ``threading.Lock``, which meant a thread
that already held it would self-deadlock the moment it called any helper that
also acquired ``db.lock`` internally. That actually happened during the
contacts feature build (squire#24): a hook inside ``_save_analysis``'s
``with db.lock:`` block called a helper that also wrapped its work in
``with db.lock:``, and the orchestrator batch-result test wedged for 20+
minutes with no output.

These tests pin the contract: ``db.lock`` is a re-entrant ``RLock`` and a
thread holding it can call helpers that also acquire it without deadlocking.
"""
import threading

import pytest

import db


def test_db_lock_is_reentrant():
    """``db.lock`` must allow the same thread to acquire it multiple times."""
    with db.lock:
        with db.lock:
            with db.lock:
                pass


@pytest.mark.timeout(5)
def test_db_helper_is_safe_to_call_under_existing_lock():
    """A helper that internally takes ``db.lock`` must not deadlock when the
    caller is already holding it. We exercise the real upsert path used by the
    contacts scraper — the same shape that triggered the original deadlock."""
    with db.lock:
        # ``upsert_contact_from_header`` writes to two tables and is the
        # canonical helper called from inside ``_save_analysis``'s lock block.
        cid = db.upsert_contact_from_header(
            display_name="Lock Test",
            email="lock-test@example.com",
            item_id="lock-test-item",
            item_timestamp="2026-04-10T00:00:00Z",
        )
        assert cid is not None


@pytest.mark.timeout(5)
def test_db_lock_releases_after_nested_acquire():
    """Another thread must be able to acquire the lock once a re-entrant
    holder has fully released it."""
    acquired = threading.Event()

    def _grab_after():
        with db.lock:
            acquired.set()

    with db.lock:
        with db.lock:
            t = threading.Thread(target=_grab_after)
            t.start()
            # The other thread is blocked while we still hold the lock.
            assert not acquired.wait(timeout=0.2)
        # Release one level — still held, so the other thread is still blocked.
        assert not acquired.wait(timeout=0.05)
    # Fully released — the other thread should now acquire it.
    assert acquired.wait(timeout=2.0)
    t.join(timeout=2.0)
