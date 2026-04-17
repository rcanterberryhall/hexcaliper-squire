"""Regression test for squire#33: parsival has no GPU-concurrency throttle.

Background
----------
parsival used to wrap every ``agent.analyze()`` call in a
``threading.Semaphore(1)`` (``orchestrator._sem``).  That dated from the
single-GPU era when parsival talked directly to Ollama; once every LLM
call started flowing through merLLM (which round-robins across all
available GPUs and runs its own tracked queue) the parsival-side throttle
became strictly subtractive — the lower of the two concurrency limits
won, and ours was hard-coded to 1, silently halving sync-path throughput
on a 2-GPU stack.

The fix in squire#33 deletes ``_sem`` and every ``with _sem:`` block.
**This test is the regression pin** that proves no other parsival-side
throttle has crept back in: it kicks two ``process_ingest_items`` calls
from two threads, blocks both inside ``analyze`` on a shared
``threading.Event``, and asserts that **both** reached the analyze stub
before either was released.  If anyone re-introduces a parsival-side
semaphore (or anything else that serialises LLM traffic on this side of
the wire), the second thread will be blocked waiting for the first one
to release, only one will register, and this test will fail.

We test against ``process_ingest_items`` because it is the simplest of
the three pipeline functions and has no global state to set up — but the
guarantee covers all three (run_scan / run_reanalyze / process_ingest_items),
because none of them now wrap the call site.
"""
import threading
import time
from unittest.mock import patch

import orchestrator
from models import RawItem, Analysis


def _raw(item_id):
    return RawItem(
        source="outlook",
        item_id=item_id,
        title=f"item {item_id}",
        body="body text",
        url="",
        author="alice@example.com",
        timestamp="2026-04-10T12:00:00+00:00",
    )


def _analysis(item_id):
    return Analysis(
        item_id=item_id, source="outlook", title=f"item {item_id}",
        author="alice", timestamp="2026-04-10T12:00:00+00:00",
        url="", has_action=False, priority="low", category="fyi",
        action_items=[], summary="S", urgency_reason=None,
    )


def test_sync_path_allows_concurrent_llm_calls():
    """Two threads must be able to be inside analyze() at the same time.

    With the old _sem in place, the second thread could not enter analyze
    until the first one released — concurrent_peak would max out at 1.
    Without any parsival-side throttle, both threads reach the stub
    simultaneously and concurrent_peak becomes 2.
    """
    in_flight = 0
    in_flight_lock = threading.Lock()
    concurrent_peak = 0
    both_arrived = threading.Event()
    release = threading.Event()
    arrivals = 0
    arrivals_lock = threading.Lock()

    def fake_analyze(item, **_kwargs):
        nonlocal in_flight, concurrent_peak, arrivals
        with in_flight_lock:
            in_flight += 1
            if in_flight > concurrent_peak:
                concurrent_peak = in_flight
        with arrivals_lock:
            arrivals += 1
            if arrivals >= 2:
                both_arrived.set()
        # Block here until the test releases us.  If parsival had a
        # semaphore, only one thread would ever reach this line — the
        # other would be stuck waiting on the lock outside analyze().
        released = release.wait(timeout=5.0)
        with in_flight_lock:
            in_flight -= 1
        assert released, "release event was never signalled — test deadlocked"
        return _analysis(item.item_id)

    # No-op the side-effects (DB writes, graph indexing, situation
    # spawning) — this test only cares about call-site concurrency.
    with patch("orchestrator.analyze", side_effect=fake_analyze), \
         patch.object(orchestrator, "_save_analysis", lambda *a, **k: None), \
         patch.object(orchestrator, "_spawn_situation_task", lambda *a, **k: None), \
         patch("orchestrator.graph.index_item", lambda *a, **k: None):

        t1 = threading.Thread(
            target=orchestrator.process_ingest_items, args=([_raw("a")],),
        )
        t2 = threading.Thread(
            target=orchestrator.process_ingest_items, args=([_raw("b")],),
        )
        t1.start()
        t2.start()

        # Wait for both threads to be inside analyze().  If parsival has
        # any kind of LLM-traffic throttle, this will time out.
        arrived = both_arrived.wait(timeout=5.0)
        try:
            assert arrived, (
                "Only one thread reached analyze() — parsival is gating "
                "LLM traffic on its own side. squire#33 says merLLM is "
                "the single source of truth for GPU concurrency; "
                "delete whatever throttle was added back."
            )
            assert concurrent_peak >= 2, (
                f"concurrent_peak={concurrent_peak}, expected >= 2"
            )
        finally:
            release.set()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)
            assert not t1.is_alive() and not t2.is_alive()


def test_single_ingest_batch_fans_out_over_merllm(monkeypatch):
    """One ``process_ingest_items`` call must fan its items out concurrently.

    parsival#75: parsival was looping items sequentially, so merLLM's
    scheduler only ever saw one outstanding job and routed every call to
    the same GPU slot. A batch of 4 items must land at least 2 analyze()
    calls in flight at once so both GPUs can be utilised.
    """
    monkeypatch.setenv("INGEST_CONCURRENCY", "4")

    in_flight = 0
    in_flight_lock = threading.Lock()
    concurrent_peak = 0
    arrivals = threading.Event()
    arrivals_count = 0
    arrivals_lock = threading.Lock()
    release = threading.Event()

    def fake_analyze(item, **_kwargs):
        nonlocal in_flight, concurrent_peak, arrivals_count
        with in_flight_lock:
            in_flight += 1
            if in_flight > concurrent_peak:
                concurrent_peak = in_flight
        with arrivals_lock:
            arrivals_count += 1
            if arrivals_count >= 2:
                arrivals.set()
        released = release.wait(timeout=5.0)
        with in_flight_lock:
            in_flight -= 1
        assert released, "release event was never signalled — test deadlocked"
        return _analysis(item.item_id)

    with patch("orchestrator.analyze", side_effect=fake_analyze), \
         patch.object(orchestrator, "_save_analysis", lambda *a, **k: None), \
         patch.object(orchestrator, "_spawn_situation_task", lambda *a, **k: None), \
         patch("orchestrator.graph.index_item", lambda *a, **k: None):

        worker = threading.Thread(
            target=orchestrator.process_ingest_items,
            args=([_raw("a"), _raw("b"), _raw("c"), _raw("d")],),
        )
        worker.start()
        try:
            assert arrivals.wait(timeout=5.0), (
                "Only one analyze() call was in flight — parsival is "
                "serialising items inside a single ingest batch. "
                "parsival#75: fan items out so merLLM sees >1 job."
            )
            assert concurrent_peak >= 2, (
                f"concurrent_peak={concurrent_peak}, expected >= 2"
            )
        finally:
            release.set()
            worker.join(timeout=5.0)
            assert not worker.is_alive()


def test_orchestrator_has_no_concurrency_semaphore():
    """Module-level guard: ``_sem`` and ``get_sem`` must stay deleted.

    A second line of defence against re-introducing the throttle.  If
    someone adds them back, this test fails immediately at import time
    instead of waiting for the threading test above to flake.
    """
    assert not hasattr(orchestrator, "_sem"), (
        "orchestrator._sem was re-introduced — see squire#33. "
        "merLLM owns GPU concurrency; parsival never gates LLM traffic."
    )
    assert not hasattr(orchestrator, "get_sem"), (
        "orchestrator.get_sem was re-introduced — see squire#33."
    )
