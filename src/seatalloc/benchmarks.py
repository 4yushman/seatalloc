"""Empirical complexity harness.

The README makes asymptotic claims. This module *measures* them, because a
complexity table nobody can reproduce is just an opinion.

Method
------
For each input size ``n`` we build a fresh engine, run one operation ``n`` (or
``k``) times, and divide total wall time by the number of operations. Each
measurement is repeated ``repeats`` times and the **minimum** is kept: the
minimum is the least noisy estimator of the underlying cost, since every source
of noise (GC, scheduler, turbo clock) can only make a run slower.

We then compare the observed growth ratio ``t(10n) / t(n)`` against what each
model predicts:

===========  =====================  =================================
Model        Ratio for a 10x jump    How to read it
===========  =====================  =================================
O(1)         1.0                    flat line
O(log n)     ~1.25 (n: 1e3 -> 1e4)  gently rising
O(n)         10x                    linear
O(n log n)   12.5x                  super-linear
===========  =====================  =================================

Run ``python -m seatalloc.benchmarks`` (or ``make bench``) to reproduce; the
output is what ``docs/BENCHMARKS.md`` is generated from.
"""

from __future__ import annotations

import json
import math
import platform
import random
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .csv_export import export_manifest, manifest_fingerprint
from .engine import SeatAllocationEngine
from .models import Attendee, EventConfig, PromotionMode
from .priority import LazyDeletionHeap
from .reference import NaiveAllocator

__all__ = ["run_all", "render_markdown", "measure", "fit_log_log_slope"]

DEFAULT_SIZES: tuple[int, ...] = (1_000, 10_000, 100_000)


# ---------------------------------------------------------------------------
# Timing primitives
# ---------------------------------------------------------------------------
def measure(fn: Callable[[], Any], *, repeats: int = 3) -> float:
    """Best-of-``repeats`` wall-clock seconds for one call of ``fn``."""
    best = math.inf
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def fit_log_log_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Least-squares slope of ``log y`` vs ``log x`` — the empirical exponent.

    A slope near ``0`` means sub-logarithmic/constant, ``1`` means linear, ``2``
    means quadratic. It is the single most useful number in a complexity report.
    """
    n = len(xs)
    if n < 2:
        return float("nan")
    lx = [math.log(x) for x in xs]
    ly = [math.log(y) for y in ys]
    mean_x = sum(lx) / n
    mean_y = sum(ly) / n
    numerator = sum((a - mean_x) * (b - mean_y) for a, b in zip(lx, ly, strict=True))
    denominator = sum((a - mean_x) ** 2 for a in lx)
    return numerator / denominator if denominator else float("nan")


def build_engine(n: int, capacity_ratio: float = 0.5, *, seed: int = 7) -> SeatAllocationEngine:
    """Register ``n`` synthetic attendees; ~half end up waitlisted."""
    capacity = max(1, int(n * capacity_ratio))
    engine = SeatAllocationEngine(EventConfig(event_id="BENCH", name="Benchmark Event", capacity=capacity))
    rng = random.Random(seed)
    tiers = ["MEMBER", "GENERAL", "VOLUNTEER", "GENERAL"]
    for i in range(n):
        engine.register(
            Attendee(
                attendee_id=f"A{i:07d}",
                name=f"Attendee {i}",
                email=f"user{i}@example.com",
                tier=tiers[rng.randrange(len(tiers))],
            ),
            registered_at=BASE_EPOCH + i * 0.5,
        )
    return engine


@dataclass
class Timing:
    """One measured operation at one input size.

    ``n`` is the *unit count* of the operation (registrations, cancellations,
    queue length...); ``tier`` is the workload size that produced it, which is
    what lets rows be aligned across operations that act on different counts.
    """

    operation: str
    n: int
    per_op_seconds: float
    total_seconds: float
    note: str = ""
    tier: int = 0

    @property
    def per_op_micros(self) -> float:
        return self.per_op_seconds * 1e6


@dataclass
class Comparison:
    """Engine vs naive model, same workload."""

    workload: str
    n: int
    engine_seconds: float
    naive_seconds: float
    speedup: float
    note: str = ""


@dataclass
class Report:
    generated_at: str
    python: str
    platform: str
    sizes: list[int]
    timings: list[Timing] = field(default_factory=list)
    comparisons: list[Comparison] = field(default_factory=list)
    slopes: dict[str, float] = field(default_factory=dict)
    invariants: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Individual experiments
# ---------------------------------------------------------------------------
BASE_EPOCH = 1_700_000_000.0


def _populate(engine: SeatAllocationEngine, n: int, *, tag: str = "A") -> None:
    """Register ``n`` attendees with unique ids (``tag`` keeps repeats distinct)."""
    for i in range(n):
        engine.register(
            Attendee(f"{tag}{i:07d}", f"Attendee {i}", f"user{i}@example.com", "GENERAL"),
            registered_at=BASE_EPOCH + i * 0.5,
        )


def _best_populate(n: int, *, repeats: int) -> float:
    """Best-of-repeats seconds to register ``n`` attendees into a fresh engine."""
    best = math.inf
    for rep in range(repeats):
        engine = SeatAllocationEngine(
            EventConfig(event_id="SCALE", name="Scaling", capacity=max(1, n // 2))
        )
        start = time.perf_counter()
        _populate(engine, n, tag=f"A{rep}")
        best = min(best, time.perf_counter() - start)
    return best


def bench_scaling(sizes: Sequence[int] = DEFAULT_SIZES, *, repeats: int = 3) -> list[Timing]:
    """Per-operation cost of the hot paths as ``n`` grows.

    Each size is measured several times into a *fresh* engine and the minimum is
    kept. Results are discarded for a warm-up run first, because the first pass
    through any workload pays interpreter/GC warm-up costs that have nothing to
    do with the algorithm (without it, ``n=10k`` can look slower than ``n=100k``).
    """
    _best_populate(2_000, repeats=1)  # warm-up, result discarded

    timings: list[Timing] = []
    for n in sizes:
        # --- registration (O(1) confirm OR O(log n) heap push) -------------
        total = _best_populate(n, repeats=repeats)
        timings.append(Timing("register", n, total / n, total,
                              "O(1) confirm or O(log n) heap push", tier=n))

        # --- cancellation that triggers a promotion -------------------------
        best = math.inf
        k = 0
        for rep in range(repeats):
            engine = SeatAllocationEngine(
                EventConfig(event_id="CANCEL", name="Cancel", capacity=max(1, n // 2))
            )
            _populate(engine, n, tag=f"C{rep}")
            victims = [r.reg_id for r in engine.confirmed()[: max(1, n // 8)]]
            k = len(victims)
            at = BASE_EPOCH + n
            start = time.perf_counter()
            for i, reg_id in enumerate(victims):
                engine.cancel(reg_id, at=at + i)
            best = min(best, time.perf_counter() - start)
        timings.append(Timing("cancel + promote", k, best / k, best,
                              "O(1) tombstone + O(log n) heappop", tier=n))

        # --- worst-case rank query (the honest O(k log k) one) --------------
        engine = SeatAllocationEngine(
            EventConfig(event_id="RANK", name="Rank", capacity=max(1, n // 2))
        )
        _populate(engine, n, tag="R")
        queue = engine.waitlist()
        if queue:
            start = time.perf_counter()
            engine.waitlist_position(queue[-1].reg_id)   # rank == queue length
            total = time.perf_counter() - start
            timings.append(Timing("rank query (worst case)", len(queue), total, total,
                                  "O(n) counting scan — rank is not a heap primitive", tier=n))

        # --- manifest export ------------------------------------------------
        out = Path("/tmp/_seatalloc_bench_manifest.csv")
        start = time.perf_counter()
        export_manifest(engine, out)
        total = time.perf_counter() - start
        timings.append(Timing("export manifest", n, total / n, total,
                              "O(n log n) sort + O(n) I/O", tier=n))
    return timings


def bench_engine_vs_naive(*, n: int = 4_000, cancels: int = 400, repeats: int = 2) -> list[Comparison]:
    """Same workload through the heap engine and the naive re-sorting model.

    Scenario: a long waitlist with many cancellations — the case where an
    ``O(n)``-per-deletion structure (or "remove + heapify") degrades badly.
    """
    capacity = max(1, n // 4)
    config = EventConfig(event_id="CMP", name="Comparison", capacity=capacity)
    attendees = [
        Attendee(f"A{i:06d}", f"Attendee {i}", f"user{i}@example.com", "GENERAL") for i in range(n)
    ]
    base = 1_700_000_000.0
    victims = list(range(0, capacity, max(1, capacity // cancels)))[:cancels]

    def run_engine() -> None:
        engine = SeatAllocationEngine(EventConfig.from_dict(config.to_dict()))
        for i, attendee in enumerate(attendees):
            engine.register(attendee, registered_at=base + i * 0.5)
        for i, idx in enumerate(victims):
            engine.cancel(f"CMP-R{idx + 1:06d}", at=base + n + i)

    def run_naive() -> None:
        model = NaiveAllocator(EventConfig.from_dict(config.to_dict()))
        for i, attendee in enumerate(attendees):
            model.register(attendee, base + i * 0.5)
        for i, idx in enumerate(victims):
            model.cancel(f"CMP-R{idx + 1:06d}", base + n + i)

    engine_seconds = measure(run_engine, repeats=repeats)
    naive_seconds = measure(run_naive, repeats=repeats)
    return [
        Comparison(
            workload=f"{n} registrations + {len(victims)} cancellations (capacity {capacity})",
            n=n,
            engine_seconds=engine_seconds,
            naive_seconds=naive_seconds,
            speedup=naive_seconds / engine_seconds if engine_seconds else float("inf"),
            note="naive re-sorts the whole waitlist per cancellation",
        )
    ]


def bench_removal_strategies(*, n: int = 50_000, removals: int = 1_000, repeats: int = 2) -> list[Comparison]:
    """Delete-the-middle-of-a-queue: heapq+heapify vs tombstone (what we ship)."""
    import heapq

    rows = [(i, f"R{i}") for i in range(n)]
    victims = random.Random(11).sample([r[1] for r in rows], removals)

    def heapify_removal() -> None:
        heap = list(rows)
        heapq.heapify(heap)
        for reg_id in victims:
            for idx, entry in enumerate(heap):
                if entry[1] == reg_id:
                    del heap[idx]
                    break
            heapq.heapify(heap)  # O(n) — the anti-pattern

    def tombstone_removal() -> None:
        heap: LazyDeletionHeap[tuple[int, str]] = LazyDeletionHeap(
            key_fn=lambda item: (item[0],), id_fn=lambda item: item[1]
        )
        heap.rebuild(rows)
        for reg_id in victims:
            heap.remove(reg_id)   # O(1)
        heap.compact()            # O(n) once, at the end

    naive = measure(heapify_removal, repeats=repeats)
    ours = measure(tombstone_removal, repeats=repeats)
    return [
        Comparison(
            workload=f"{removals} removals from a {n:,}-entry queue",
            n=n,
            engine_seconds=ours,
            naive_seconds=naive,
            speedup=naive / ours if ours else float("inf"),
            note="O(1) tombstone vs O(n) re-heapify per deletion",
        )
    ]


def bench_heapify_vs_pushloop(*, n: int = 200_000, repeats: int = 2) -> list[Comparison]:
    """``heapify`` is O(n); pushing one element at a time is O(n log n)."""
    import heapq
    import random as _random

    # Shuffled on purpose — see tests/test_complexity.py for why sorted input
    # would flatter the push loop and hide heapify's O(n) advantage.
    data = [(i,) for i in _random.Random(3).sample(range(n * 4), n)]

    def push_loop() -> None:
        heap: list[tuple[int]] = []
        for item in data:
            heapq.heappush(heap, item)

    def bulk_heapify() -> None:
        heap: list[tuple[int]] = list(data)
        heapq.heapify(heap)

    push_seconds = measure(push_loop, repeats=repeats)
    heapify_seconds = measure(bulk_heapify, repeats=repeats)
    return [
        Comparison(
            workload=f"build a {n:,}-element heap",
            n=n,
            engine_seconds=heapify_seconds,
            naive_seconds=push_seconds,
            speedup=push_seconds / heapify_seconds if heapify_seconds else float("inf"),
            note="heapify O(n) vs n heappush O(n log n)",
        )
    ]


def bench_offer_mode(*, n: int = 20_000, repeats: int = 3) -> list[Timing]:
    """OFFER-mode promotion + TTL sweep (the realistic 'accept within 30 min' flow)."""
    capacity = max(1, n // 10)
    engine = SeatAllocationEngine(
        EventConfig(event_id="OFFER", name="Offer mode", capacity=capacity,
                    promotion_mode=PromotionMode.OFFER, offer_ttl_seconds=600)
    )
    base = 1_700_000_000.0
    for i in range(n):
        engine.register(
            Attendee(f"A{i:06d}", f"A {i}", f"u{i}@example.com", "GENERAL"), registered_at=base + i
        )
    churn = min(2_000, capacity)
    winners = [r.reg_id for r in engine.confirmed()[:churn]] or [
        r.reg_id for r in engine.offers()[:churn]
    ]

    def churn_cycle() -> None:
        for i, reg_id in enumerate(winners):
            engine.cancel(reg_id, at=base + n + i)
            engine.expire_offers(at=base + n + i + 700)

    total = measure(churn_cycle, repeats=repeats)
    return [
        Timing("cancel + offer + TTL expiry sweep", len(winners), total / max(1, len(winners)), total,
               "each churn frees a seat, offers it, then recycles it", tier=n)
    ]


def correctness_spot_checks() -> dict[str, Any]:
    """Small deterministic checks printed alongside the timings."""
    engine = build_engine(5_000, capacity_ratio=0.4)
    fingerprint_a = manifest_fingerprint(engine)
    rebuilt = build_engine(5_000, capacity_ratio=0.4)
    fingerprint_b = manifest_fingerprint(rebuilt)
    confirmed = engine.confirmed()
    seats = [r.seat_number for r in confirmed]
    return {
        "reconcile_violations": engine.reconcile(),
        "manifest_fingerprint": fingerprint_a,
        "deterministic_rebuild_matches": fingerprint_a == fingerprint_b,
        "unique_seat_numbers": len(seats) == len(set(seats)),
        "held_within_capacity": engine.held_seats() <= engine.capacity,
        "waitlist_heap_array": engine._waitlist.heap_size,  # noqa: SLF001 - metric
        "tombstone_debt": engine._waitlist.stale_entries,   # noqa: SLF001 - metric
    }


# ---------------------------------------------------------------------------
# Orchestration / reporting
# ---------------------------------------------------------------------------
def run_all(sizes: Sequence[int] = DEFAULT_SIZES, *, repeats: int = 3) -> Report:
    timings = bench_scaling(sizes, repeats=repeats)
    comparisons = [
        *bench_engine_vs_naive(repeats=repeats),
        *bench_removal_strategies(repeats=repeats),
        *bench_heapify_vs_pushloop(repeats=repeats),
    ]
    timings.extend(bench_offer_mode(repeats=repeats))

    slopes: dict[str, float] = {}
    for operation in {t.operation for t in timings}:
        series = sorted((t for t in timings if t.operation == operation), key=lambda t: t.n)
        if len(series) >= 2 and all(t.n for t in series):
            slopes[operation] = round(
                fit_log_log_slope([t.n for t in series], [t.per_op_seconds for t in series]), 4
            )

    return Report(
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        python=sys.version.split()[0],
        platform=f"{platform.system()} {platform.machine()} / {platform.processor() or 'unknown CPU'}",
        sizes=list(sizes),
        timings=timings,
        comparisons=comparisons,
        slopes=slopes,
        invariants=correctness_spot_checks(),
    )


def render_markdown(report: Report) -> str:
    lines: list[str] = []
    add = lines.append
    add("# Empirical Benchmarks")
    add("")
    add(f"Generated: **{report.generated_at}**  ")
    add(f"Python: **{report.python}**  ")
    add(f"Machine: **{report.platform}**  ")
    add("")
    add(
        "Reproduce with `make bench` (or `python -m seatalloc.benchmarks`). "
        "Each measurement runs into a **fresh engine**, several times, after a discarded "
        "warm-up pass, and the **minimum** is reported: every source of noise "
        "(GC, scheduler, CPU frequency) can only make a run slower, so the minimum is the "
        "least-biased estimator of the underlying cost. Absolute numbers vary by machine; "
        "the *growth ratio* and the log-log exponent are the signal."
    )
    add("")

    add("## 1. Per-operation cost as the event grows")
    add("")
    scaled = [t for t in report.timings if t.tier in set(report.sizes)]
    ops = list(dict.fromkeys(t.operation for t in scaled))
    ops = [op for op in ops if len({t.tier for t in scaled if t.operation == op}) == len(report.sizes)]
    add("| Event size (registrations) | " + " | ".join(ops) + " |")
    add("|---" * (len(ops) + 1) + "|")
    for tier in report.sizes:
        cells = []
        for op in ops:
            row = next(t for t in scaled if t.operation == op and t.tier == tier)
            value = row.per_op_micros
            cells.append(f"{value / 1000:.2f} ms" if value >= 1000 else f"{value:.3f} µs")
        add(f"| {tier:,} | " + " | ".join(cells) + " |")
    growth_cells = []
    for op in ops:
        series = [t for t in scaled if t.operation == op]
        series.sort(key=lambda t: t.tier)
        ratio = series[-1].per_op_seconds / series[0].per_op_seconds
        exponent = report.slopes.get(op, float("nan"))
        growth_cells.append(f"**{ratio:.2f}x**  (exp {exponent:+.3f})")
    add("| **growth (100k / 1k)** | " + " | ".join(growth_cells) + " |")
    add("")
    add("Every column is a per-operation cost, so a *flat or gently rising* row means the")
    add("operation does not get slower as the event fills up. The last row gives the change")
    add("between the smallest and largest workload; for reference, `O(1)` predicts 1.0x,")
    add("`O(log n)` ≈ 1.25x, `O(n)` 10x and `O(n log n)` ≈ 12.5x for a 1,000 → 100,000 jump.")
    add("")
    add("**Reading the exponent** (least-squares slope of log(per-op time) vs log(n)):")
    add("`~0.00` = constant, `~0.10-0.25` = logarithmic, `~1.0` = linear, `~2.0` = quadratic.")
    add("A true `O(log n)` cost is sub-linear even on a log-log plot, so it shows up as a")
    add("small, slowly-rising value rather than a flat line.")
    add("")
    add("Workloads behind each column (queue depth grows with the event):")
    add("")
    for op in ops:
        note = next(t.note for t in scaled if t.operation == op)
        units = [t.n for t in sorted((t for t in scaled if t.operation == op), key=lambda t: t.tier)]
        add(f"* `{op}` — {note}; unit counts measured: {', '.join(f'{u:,}' for u in units)}")
    add("")
    add("Single-scenario measurement (not size-scaled):")
    add("")
    for t in report.timings:
        if t.tier not in set(report.sizes):
            add(f"* `{t.operation}` at n={t.n:,}: **{t.per_op_micros / 1000:.3f} ms** per operation "
                f"({t.total_seconds:.3f}s total) — {t.note}")
    add("")

    add("## 2. Heap engine vs the naive baseline")
    add("")
    add("| Workload | Heap engine | Naive model | Speed-up | Why |")
    add("|---|---|---|---|---|")
    for c in report.comparisons:
        add(
            f"| {c.workload} | {c.engine_seconds * 1000:.1f} ms | "
            f"{c.naive_seconds * 1000:.1f} ms | **{c.speedup:.1f}x** | {c.note} |"
        )
    add("")

    add("## 3. Invariant checks run alongside the benchmark")
    add("")
    add("| Check | Result |")
    add("|---|---|")
    for key, value in report.invariants.items():
        add(f"| `{key}` | `{value}` |")
    add("")

    add("## 4. Raw JSON")
    add("")
    add("```json")
    add(report.to_json())
    add("```")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = Path(argv[0]) if argv else Path("docs/BENCHMARKS.md")
    report = run_all()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(report), encoding="utf-8")
    Path("benchmarks").mkdir(exist_ok=True)
    Path("benchmarks/results.json").write_text(report.to_json(), encoding="utf-8")
    print(render_markdown(report))
    print(f"\nWrote {out} and benchmarks/results.json")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
