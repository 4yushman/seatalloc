# Empirical Benchmarks

Generated: **2026-09-10 06:17:58 UTC**  
Python: **3.13.14**  
Machine: **Linux x86_64 / unknown CPU**  

Reproduce with `make bench` (or `python -m seatalloc.benchmarks`). Each measurement runs into a **fresh engine**, several times, after a discarded warm-up pass, and the **minimum** is reported: every source of noise (GC, scheduler, CPU frequency) can only make a run slower, so the minimum is the least-biased estimator of the underlying cost. Absolute numbers vary by machine; the *growth ratio* and the log-log exponent are the signal.

## 1. Per-operation cost as the event grows

| Event size (registrations) | register | cancel + promote | rank query (worst case) | export manifest |
|---|---|---|---|---|
| 1,000 | 7.630 µs | 6.533 µs | 65.287 µs | 5.365 µs |
| 10,000 | 10.088 µs | 8.244 µs | 588.456 µs | 5.026 µs |
| 100,000 | 15.397 µs | 18.575 µs | 6.41 ms | 6.832 µs |
| **growth (100k / 1k)** | **2.02x**  (exp +0.152) | **2.84x**  (exp +0.227) | **98.11x**  (exp +0.996) | **1.27x**  (exp +0.052) |

Every column is a per-operation cost, so a *flat or gently rising* row means the
operation does not get slower as the event fills up. The last row gives the change
between the smallest and largest workload; for reference, `O(1)` predicts 1.0x,
`O(log n)` ≈ 1.25x, `O(n)` 10x and `O(n log n)` ≈ 12.5x for a 1,000 → 100,000 jump.

**Reading the exponent** (least-squares slope of log(per-op time) vs log(n)):
`~0.00` = constant, `~0.10-0.25` = logarithmic, `~1.0` = linear, `~2.0` = quadratic.
A true `O(log n)` cost is sub-linear even on a log-log plot, so it shows up as a
small, slowly-rising value rather than a flat line.

Workloads behind each column (queue depth grows with the event):

* `register` — O(1) confirm or O(log n) heap push; unit counts measured: 1,000, 10,000, 100,000
* `cancel + promote` — O(1) tombstone + O(log n) heappop; unit counts measured: 125, 1,250, 12,500
* `rank query (worst case)` — O(n) counting scan — rank is not a heap primitive; unit counts measured: 500, 5,000, 50,000
* `export manifest` — O(n log n) sort + O(n) I/O; unit counts measured: 1,000, 10,000, 100,000

Single-scenario measurement (not size-scaled):

* `cancel + offer + TTL expiry sweep` at n=2,000: **2.806 ms** per operation (5.613s total) — each churn frees a seat, offers it, then recycles it

## 2. Heap engine vs the naive baseline

| Workload | Heap engine | Naive model | Speed-up | Why |
|---|---|---|---|---|
| 4000 registrations + 400 cancellations (capacity 1000) | 60.0 ms | 4804.8 ms | **80.0x** | naive re-sorts the whole waitlist per cancellation |
| 1000 removals from a 50,000-entry queue | 45.5 ms | 6753.0 ms | **148.6x** | O(1) tombstone vs O(n) re-heapify per deletion |
| build a 200,000-element heap | 36.1 ms | 59.8 ms | **1.7x** | heapify O(n) vs n heappush O(n log n) |

## 3. Invariant checks run alongside the benchmark

| Check | Result |
|---|---|
| `reconcile_violations` | `[]` |
| `manifest_fingerprint` | `a2ac9a1df7eea4b01168f6887f0458b61b382c133a5b8cd1b0301a47308c6cea` |
| `deterministic_rebuild_matches` | `True` |
| `unique_seat_numbers` | `True` |
| `held_within_capacity` | `True` |
| `waitlist_heap_array` | `3000` |
| `tombstone_debt` | `0` |

## 4. Raw JSON

```json
{
  "comparisons": [
    {
      "engine_seconds": 0.060022879999905854,
      "n": 4000,
      "naive_seconds": 4.80478648899998,
      "note": "naive re-sorts the whole waitlist per cancellation",
      "speedup": 80.04924936969896,
      "workload": "4000 registrations + 400 cancellations (capacity 1000)"
    },
    {
      "engine_seconds": 0.04545286099983059,
      "n": 50000,
      "naive_seconds": 6.752985107999848,
      "note": "O(1) tombstone vs O(n) re-heapify per deletion",
      "speedup": 148.57117812726943,
      "workload": "1000 removals from a 50,000-entry queue"
    },
    {
      "engine_seconds": 0.03605117600000085,
      "n": 200000,
      "naive_seconds": 0.059833161999904405,
      "note": "heapify O(n) vs n heappush O(n log n)",
      "speedup": 1.6596729604577392,
      "workload": "build a 200,000-element heap"
    }
  ],
  "generated_at": "2026-09-10 06:17:58 UTC",
  "invariants": {
    "deterministic_rebuild_matches": true,
    "held_within_capacity": true,
    "manifest_fingerprint": "a2ac9a1df7eea4b01168f6887f0458b61b382c133a5b8cd1b0301a47308c6cea",
    "reconcile_violations": [],
    "tombstone_debt": 0,
    "unique_seat_numbers": true,
    "waitlist_heap_array": 3000
  },
  "platform": "Linux x86_64 / unknown CPU",
  "python": "3.13.14",
  "sizes": [
    1000,
    10000,
    100000
  ],
  "slopes": {
    "cancel + promote": 0.2269,
    "export manifest": 0.0525,
    "rank query (worst case)": 0.9959,
    "register": 0.1525
  },
  "timings": [
    {
      "n": 1000,
      "note": "O(1) confirm or O(log n) heap push",
      "operation": "register",
      "per_op_seconds": 7.629648000147426e-06,
      "tier": 1000,
      "total_seconds": 0.0076296480001474265
    },
    {
      "n": 125,
      "note": "O(1) tombstone + O(log n) heappop",
      "operation": "cancel + promote",
      "per_op_seconds": 6.532735998916905e-06,
      "tier": 1000,
      "total_seconds": 0.0008165919998646132
    },
    {
      "n": 500,
      "note": "O(n) counting scan \u2014 rank is not a heap primitive",
      "operation": "rank query (worst case)",
      "per_op_seconds": 6.528700032504275e-05,
      "tier": 1000,
      "total_seconds": 6.528700032504275e-05
    },
    {
      "n": 1000,
      "note": "O(n log n) sort + O(n) I/O",
      "operation": "export manifest",
      "per_op_seconds": 5.365146000258391e-06,
      "tier": 1000,
      "total_seconds": 0.005365146000258392
    },
    {
      "n": 10000,
      "note": "O(1) confirm or O(log n) heap push",
      "operation": "register",
      "per_op_seconds": 1.0087582799997108e-05,
      "tier": 10000,
      "total_seconds": 0.10087582799997108
    },
    {
      "n": 1250,
      "note": "O(1) tombstone + O(log n) heappop",
      "operation": "cancel + promote",
      "per_op_seconds": 8.243607999975211e-06,
      "tier": 10000,
      "total_seconds": 0.010304509999969014
    },
    {
      "n": 5000,
      "note": "O(n) counting scan \u2014 rank is not a heap primitive",
      "operation": "rank query (worst case)",
      "per_op_seconds": 0.0005884559996047756,
      "tier": 10000,
      "total_seconds": 0.0005884559996047756
    },
    {
      "n": 10000,
      "note": "O(n log n) sort + O(n) I/O",
      "operation": "export manifest",
      "per_op_seconds": 5.025998400014942e-06,
      "tier": 10000,
      "total_seconds": 0.050259984000149416
    },
    {
      "n": 100000,
      "note": "O(1) confirm or O(log n) heap push",
      "operation": "register",
      "per_op_seconds": 1.5397418389998165e-05,
      "tier": 100000,
      "total_seconds": 1.5397418389998165
    },
    {
      "n": 12500,
      "note": "O(1) tombstone + O(log n) heappop",
      "operation": "cancel + promote",
      "per_op_seconds": 1.8574562560024787e-05,
      "tier": 100000,
      "total_seconds": 0.23218203200030985
    },
    {
      "n": 50000,
      "note": "O(n) counting scan \u2014 rank is not a heap primitive",
      "operation": "rank query (worst case)",
      "per_op_seconds": 0.006405598000128521,
      "tier": 100000,
      "total_seconds": 0.006405598000128521
    },
    {
      "n": 100000,
      "note": "O(n log n) sort + O(n) I/O",
      "operation": "export manifest",
      "per_op_seconds": 6.831834540003001e-06,
      "tier": 100000,
      "total_seconds": 0.6831834540003001
    },
    {
      "n": 2000,
      "note": "each churn frees a seat, offers it, then recycles it",
      "operation": "cancel + offer + TTL expiry sweep",
      "per_op_seconds": 0.0028063865859999167,
      "tier": 20000,
      "total_seconds": 5.612773171999834
    }
  ]
}
```
