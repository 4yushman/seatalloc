"""Empirical complexity tests.

Asymptotics are a claim about *growth*, so these tests assert **growth ratios**
rather than absolute milliseconds. A ratio bound of 5x for a 10x input increase
is deliberately generous: it comfortably accommodates `O(log n)` (≈1.25x) and
even `O(sqrt n)`-ish noise, while still failing loudly if a comparison-based
structure degrades to `O(n)` (10x) or `O(n²)` (100x).

The tests marked ``slow`` run the larger workloads; skip them with
``pytest -m "not slow"``.
"""

from __future__ import annotations

import heapq
import random
import time

import pytest

from seatalloc import Attendee, EventConfig, LazyDeletionHeap, SeatAllocationEngine
from seatalloc.benchmarks import measure
from seatalloc.csv_export import export_manifest
from seatalloc.reference import NaiveAllocator

pytestmark = pytest.mark.slow


def _populate(engine: SeatAllocationEngine, n: int, base: float = 1_700_000_000.0) -> None:
    for i in range(n):
        engine.register(
            Attendee(f"A{i:07d}", f"Attendee {i}", f"u{i}@example.com", "GENERAL"),
            registered_at=base + i * 0.5,
        )


def _time_populate(n: int) -> float:
    """Seconds to register ``n`` attendees, excluding engine construction.

    A fresh engine per measurement keeps attendee ids unique (the duplicate
    guard would otherwise reject everything on a repeat) and keeps the O(n)
    seat-pool setup out of the timed region.
    """
    engine = SeatAllocationEngine(EventConfig(event_id=f"R{n}", capacity=max(1, n // 2)))
    start = time.perf_counter()
    _populate(engine, n)
    return time.perf_counter() - start


def _time_cancellations(n: int) -> tuple[float, int]:
    """Seconds to cancel ``n/20`` confirmed seats, each triggering a promotion."""
    engine = SeatAllocationEngine(EventConfig(event_id=f"C{n}", capacity=max(2, n // 2)))
    _populate(engine, n)
    victims = [r.reg_id for r in engine.confirmed()[: max(1, n // 20)]]
    start = time.perf_counter()
    for i, reg_id in enumerate(victims):
        engine.cancel(reg_id, at=1_800_000_000.0 + i)
    return time.perf_counter() - start, len(victims)


def test_registration_cost_grows_logarithmically_not_linearly() -> None:
    small = _time_populate(5_000) / 5_000
    large = _time_populate(50_000) / 50_000
    ratio = large / small
    # 10x the registrations must not cost 10x per registration.
    assert ratio < 5, f"register cost grew {ratio:.1f}x for a 10x input — expected ~1.2x"


def test_cancellation_with_promotion_grows_sublinearly() -> None:
    small_total, small_k = _time_cancellations(5_000)
    large_total, large_k = _time_cancellations(50_000)
    ratio = (large_total / large_k) / (small_total / small_k)
    assert ratio < 5, f"cancel+promote grew {ratio:.1f}x for a 10x input — expected ~1.3x"


def test_manifest_export_is_n_log_n_not_quadratic() -> None:
    engine_small = SeatAllocationEngine(EventConfig(event_id="E1", capacity=1000))
    _populate(engine_small, 10_000)
    engine_large = SeatAllocationEngine(EventConfig(event_id="E2", capacity=1000))
    _populate(engine_large, 100_000)

    small = measure(lambda: export_manifest(engine_small, "/tmp/_s.csv", at=0.0), repeats=2)
    large = measure(lambda: export_manifest(engine_large, "/tmp/_l.csv", at=0.0), repeats=2)
    ratio = large / small
    # 10x rows => ~12x time for O(n log n); >50x would signal an O(n^2) exporter.
    assert ratio < 25, f"export grew {ratio:.1f}x for 10x rows (expected ~12x for n log n)"


def test_heapify_bulk_build_beats_n_individual_pushes() -> None:
    n = 300_000
    data = [(i, f"k{i}") for i in range(n)]
    # Shuffle on purpose: pushing *already sorted* values into a min-heap is
    # O(1) amortised per item (each new item bubbles nowhere), so a sorted
    # input would make the push loop look linear and hide heapify's advantage.
    random.Random(5).shuffle(data)

    def push_loop() -> None:
        heap: list[tuple[int, str]] = []
        for item in data:
            heapq.heappush(heap, item)

    def bulk() -> None:
        heap = list(data)
        heapq.heapify(heap)

    push_seconds = measure(push_loop, repeats=2)
    bulk_seconds = measure(bulk, repeats=2)
    assert bulk_seconds < push_seconds, (
        f"O(n) heapify ({bulk_seconds:.4f}s) should beat n heappush "
        f"({push_seconds:.4f}s)"
    )


def test_tombstone_deletion_beats_remove_and_reheapify() -> None:
    n, removals = 30_000, 500
    victims = [f"R{i}" for i in range(0, removals)]

    def naive_removal() -> None:
        heap = [(i, f"R{i}") for i in range(n)]
        heapq.heapify(heap)
        for reg_id in victims:
            for idx, entry in enumerate(heap):
                if entry[1] == reg_id:
                    del heap[idx]
                    break
            heapq.heapify(heap)          # O(n) per deletion

    def tombstone() -> None:
        heap: LazyDeletionHeap[tuple[int, str]] = LazyDeletionHeap(
            key_fn=lambda item: (item[0],), id_fn=lambda item: item[1]
        )
        heap.rebuild([(i, f"R{i}") for i in range(n)])
        for reg_id in victims:
            heap.remove(reg_id)          # O(1) per deletion
        heap.compact()                   # one O(n) pass

    naive_seconds = measure(naive_removal, repeats=2)
    ours = measure(tombstone, repeats=2)
    assert ours < naive_seconds, (
        f"tombstone deletions ({ours:.4f}s) should beat remove+heapify ({naive_seconds:.4f}s)"
    )


def test_heap_engine_beats_the_naive_model_on_churn() -> None:
    n, capacity = 3_000, 500
    attendees = [
        Attendee(f"A{i:06d}", f"A {i}", f"a{i}@example.com", "GENERAL") for i in range(n)
    ]
    victims = [f"CMP-R{i + 1:06d}" for i in range(capacity // 2)]

    def engine_run() -> None:
        engine = SeatAllocationEngine(EventConfig(event_id="CMP", capacity=capacity))
        for i, attendee in enumerate(attendees):
            engine.register(attendee, registered_at=1_700_000_000.0 + i)
        for i, reg_id in enumerate(victims):
            engine.cancel(reg_id, at=1_800_000_000.0 + i)

    def naive_run() -> None:
        model = NaiveAllocator(EventConfig(event_id="CMP", capacity=capacity))
        for i, attendee in enumerate(attendees):
            model.register(attendee, 1_700_000_000.0 + i)
        for i, reg_id in enumerate(victims):
            model.cancel(reg_id, 1_800_000_000.0 + i)

    engine_seconds = measure(engine_run, repeats=2)
    naive_seconds = measure(naive_run, repeats=2)
    assert engine_seconds < naive_seconds


def test_position_query_is_linear_in_the_queue_not_quadratic() -> None:
    """Rank is not a heap primitive; the docs promise O(n), so prove it."""
    engine = SeatAllocationEngine(EventConfig(event_id="POS", capacity=1))
    _populate(engine, 20_000)
    queue = engine.waitlist()
    assert engine.waitlist_position(queue[0].reg_id) == 1
    assert engine.waitlist_position(queue[-1].reg_id) == len(queue)

    # Both ends cost the same single scan (no early exit), which is precisely
    # why the export/UI path prefers engine.waitlist() (one O(n log n) sort)
    # over n rank queries.
    near = measure(lambda: engine.waitlist_position(queue[0].reg_id), repeats=3)
    far = measure(lambda: engine.waitlist_position(queue[-1].reg_id), repeats=3)
    assert near <= far * 2


def test_memory_metric_is_exposed_for_tombstone_debt() -> None:
    engine = SeatAllocationEngine(EventConfig(event_id="MEM", capacity=100))
    _populate(engine, 5_000)
    for reg in engine.registrations[:1_000]:
        if reg.status.name == "WAITLISTED":
            engine.cancel(reg.reg_id, at=0.0)
    stats = engine.stats()
    # Space is O(n) live + O(cancellations) tombstones, and both are observable.
    assert stats["heap_array_size"] == stats["waitlisted"] + stats["tombstone_debt"]
