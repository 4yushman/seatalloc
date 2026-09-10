"""Unit tests for the heap that everything else is built on.

These tests are deliberately adversarial about the *lazy deletion* trick: the
whole point of the design is that a deleted element is still sitting in the
array, so we assert both the observable behaviour (pop never returns a deleted
item) and the internal accounting (tombstone debt is reclaimed by compact()).
"""

from __future__ import annotations

import heapq
import random

import pytest

from seatalloc.priority import LazyDeletionHeap


def make_heap() -> LazyDeletionHeap[tuple[int, str]]:
    return LazyDeletionHeap(key_fn=lambda item: (item[0],), id_fn=lambda item: item[1])


def test_pops_in_priority_order() -> None:
    heap = make_heap()
    for priority, key in [(5, "e"), (1, "a"), (3, "c"), (2, "b"), (4, "d")]:
        heap.push((priority, key))
    assert [heap.pop()[1] for _ in range(5)] == ["a", "b", "c", "d", "e"]
    assert heap.pop() is None


def test_len_tracks_live_items_not_array_slots() -> None:
    heap = make_heap()
    for i in range(10):
        heap.push((i, f"k{i}"))
    assert len(heap) == 10 and heap.heap_size == 10

    for i in range(4):
        assert heap.remove(f"k{i}") is True
    assert len(heap) == 6           # live items
    assert heap.heap_size == 10     # tombstones still occupy slots
    assert heap.stale_entries == 4


def test_removed_items_are_never_returned() -> None:
    heap = make_heap()
    for i in range(20):
        heap.push((i, f"k{i}"))
    for i in range(0, 20, 2):        # delete every even key
        heap.remove(f"k{i}")
    seen = []
    while (item := heap.pop()) is not None:
        seen.append(item[1])
    assert seen == [f"k{i}" for i in range(1, 20, 2)]


def test_remove_missing_key_is_false_and_harmless() -> None:
    heap = make_heap()
    heap.push((1, "a"))
    assert heap.remove("nope") is False
    assert heap.peek()[1] == "a"
    assert heap.remove("a") is True
    assert heap.remove("a") is False


def test_tombstone_debt_is_reclaimed_by_compact() -> None:
    heap = make_heap()
    for i in range(50):
        heap.push((i, f"k{i}"))
    for i in range(40):
        heap.remove(f"k{i}")
    removed = heap.compact()
    assert removed == 40
    assert heap.heap_size == 10 and heap.stale_entries == 0
    assert [heap.pop()[1] for _ in range(10)] == [f"k{i}" for i in range(40, 50)]


def test_peek_skips_tombstones_that_reach_the_root() -> None:
    heap = make_heap()
    heap.push((1, "a"))
    heap.push((2, "b"))
    heap.remove("a")                  # tombstone is now at the root
    assert heap.peek()[1] == "b"
    assert heap.pop()[1] == "b"


def test_duplicate_key_is_rejected() -> None:
    heap = make_heap()
    heap.push((1, "a"))
    with pytest.raises(KeyError):
        heap.push((2, "a"))


def test_ordered_items_is_sorted_by_priority() -> None:
    heap = make_heap()
    rng = random.Random(1)
    items = [(rng.randrange(100), f"k{i}") for i in range(100)]
    for item in items:
        heap.push(item)
    ordered = heap.ordered_items()
    assert [i[0] for i in ordered] == sorted(i[0] for i in items)
    assert len(heap) == 100  # ordering is non-destructive


def test_position_of_reports_one_based_rank() -> None:
    heap = make_heap()
    for priority, key in [(10, "z"), (1, "a"), (7, "m"), (3, "c")]:
        heap.push((priority, key))
    assert heap.position_of("a") == 1
    assert heap.position_of("c") == 2
    assert heap.position_of("m") == 3
    assert heap.position_of("z") == 4
    assert heap.position_of("ghost") is None


def test_iter_items_matches_ordered_items_and_leaves_heap_intact() -> None:
    heap = make_heap()
    for i in range(15):
        heap.push((15 - i, f"k{i}"))
    streamed = list(heap.iter_items())
    assert streamed == heap.ordered_items()
    assert len(heap) == 15


def test_rebuild_matches_repeated_push() -> None:
    items = [(i % 7, f"k{i}") for i in range(64)]
    a = make_heap()
    a.rebuild(items)
    b = make_heap()
    for item in items:
        b.push(item)
    assert list(a.iter_items()) == list(b.iter_items())


def test_equivalence_with_a_textbook_heap_under_random_ops() -> None:
    """Model check: our heap must agree with heapq + lazy-deletion bookkeeping."""
    rng = random.Random(2024)
    heap = make_heap()
    reference: list[tuple[int, str]] = []
    alive: dict[str, int] = {}

    for step in range(500):
        action = rng.choices(["push", "remove", "pop"], weights=[6, 3, 1])[0]
        if action == "push" or not alive:
            key = f"k{step}"
            # Unique priorities keep the comparison "priority order only":
            # equal priorities are broken by insertion order in our heap and by
            # key order in the reference, which would be a false mismatch.
            priority = step
            heap.push((priority, key))
            heapq.heappush(reference, (priority, key))
            alive[key] = priority
        elif action == "remove":
            key = rng.choice(list(alive))
            assert heap.remove(key) is True
            del alive[key]
        else:
            expected = heapq.heappop(reference)
            while expected[1] not in alive:
                expected = heapq.heappop(reference)
            del alive[expected[1]]
            assert heap.pop() == expected

    # Drain and confirm identical orders.
    expected_order = []
    while reference:
        item = heapq.heappop(reference)
        if item[1] in alive:
            del alive[item[1]]
            expected_order.append(item)
    assert list(heap.iter_items()) == expected_order
