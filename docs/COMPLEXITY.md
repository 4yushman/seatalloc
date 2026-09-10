# Complexity Analysis

Full reasoning behind the summary table in the README. Notation:

* **n** — registrations ever recorded
* **q** — current waitlist depth (`q ≤ n`)
* **c** — event capacity (held seats)
* **k** — number of attendees promoted by an operation
* **t** — tombstone count (`≤` number of cancellations)

---

## 1. Data structures and their primitives

| Structure | Purpose | Primitive costs |
|---|---|---|
| Waitlist min-heap (`heapq` over a list) | "who is next in line" | push `O(log q)`, peek `O(1)`, pop `O(log q)`, delete-by-key `O(1)` (tombstone), build `O(q)` |
| Entry index (`dict[key → entry]`) | membership + tombstoning | `O(1)` average |
| Free-seat min-heap | lowest free seat number | push/pop `O(log c)`, build `O(c)` |
| Registration store (`dict[reg_id → Registration]`) | lookup, iteration | `O(1)` average, `O(n)` iteration |
| Attendee index (`dict[attendee_id → reg_id]`) | duplicate detection | `O(1)` average |
| Running counters (`int`, `dict[status → int]`) | `held_seats()`, status tallies | `O(1)` |
| Audit log (append-only list + JSONL file) | replay, explanation | append `O(1)` amortised |

Everything the engine owns is one of these six; there is no hidden linear scan on a hot path
(and `tests/test_complexity.py` exists to keep it that way).

---

## 2. Per-operation analysis

### 2.1 `register(attendee, at)`

| Step | Cost |
|---|---|
| Window check, duplicate check | `O(1)` |
| Capacity check (`held_seats()`, counter) | `O(1)` |
| Create + index the registration | `O(1)` amortised |
| **Seat free** → pop from the seat pool, set `CONFIRMED` | `O(log c)` |
| **Full** → push onto the waitlist heap | `O(log q)` |
| Queue-depth recording (`len`) | `O(1)` |
| Audit append | `O(1)` amortised |
| **Total** | **`O(log n)`** |

### 2.2 `cancel(reg_id, at)`

| Step | Cost |
|---|---|
| Lookup | `O(1)` |
| Tombstone the queue entry (if queued) | `O(1)` |
| Release the seat number (push to pool) | `O(log c)` |
| Status transition + counter update | `O(1)` |
| `_fill_open_seats` → one `O(1)` peek + one `O(log q)` pop + one `O(log c)` push | `O(log n)` |
| **Total** | **`O(log n)`** |

The key win: deleting from the *middle* of the queue is `O(1)`, not `O(n)`. A cancellation
storm of `m` cancellations costs `O(m log n)` instead of `O(m·n)`.

> Amortisation note: each tombstone is eventually popped at `O(log n)` if it surfaces at the
> root before the process ends, so the strict accounting is "`O(1)` now, `O(log n)` later".
> For `m` deletions followed by draining the queue, total cost is `O(m log n)` — the same as
> deleting each element properly, but with none of the `O(n)` rewrites in between.

### 2.3 `accept_offer` / `decline_offer`

* `accept_offer`: status flip, `O(1)` — the seat was already held when the offer was made.
* `decline_offer`: `O(log c)` release + `O(log n)` promotion.

Reserving the seat *at offer time* is what makes acceptance cheap and, more importantly, what
guarantees the seat cannot be promised to two people while an offer is outstanding.

### 2.4 `expire_offers(at)`

Scans the outstanding offers (`o ≤ c`, since each offer holds one seat), so
`O(o log n)` including re-promotions. Bounded by capacity, not by n — the sweep is safe to run
on a cron job regardless of how many registrations the event accumulated.

### 2.5 `set_capacity(new_capacity)`

* Grow by `Δ`: `Δ` pushes → `O(Δ log c)`, plus `k` promotions → `O(k log q)`.
* Shrink (still `≥ held`): one filter pass over the free-seat list → `O(c)` worst case
  (only free seats exist at that point, so the pass is short in practice).
* Shrink below `held`: **rejected** with `CapacityConflictError`, `O(1)`.

### 2.6 Queries

| Query | Cost | Comment |
|---|---|---|
| `open_seats()`, `held_seats()`, `waitlist_size()` | `O(1)` | maintained counters / `len` |
| `waitlist_position(reg_id)` | `O(q)` | counting scan; see §5 |
| `confirmed()` | `O(n + c log c)` | filter + sort by seat |
| `waitlist()` | `O(q log q)` | a heap does not store a sorted array |
| `stats()` | `O(n)` | wait-time statistics need a scan; an analytics call |
| `reconcile()` | `O(n log n)` | offline audit |
| `manifest_fingerprint()` | `O(n log n)` | render + SHA-256 |

### 2.7 Exports

`export_manifest` / `export_waitlist` / `export_audit` / `export_summary` are dominated by the
`O(n log n)` sort that converts heap order into seat/queue order; rows are streamed to
`csv.DictWriter` one at a time, so extra space is `O(1)` per row. Measured: ~5-7 µs per row,
growing 1.27x for a 100x larger event (i.e. `O(1)` per row in practice — see
[BENCHMARKS.md](BENCHMARKS.md)).

### 2.8 Replay

`replay(log)` re-applies n primary events at `O(n log n)` total, allocating `O(n)`.
Because ordering derives only from recorded timestamps and the mapping is total, the rebuilt
state is identical — the property test compares the full state snapshot.

---

## 3. Comparison with alternative implementations

Let **m** be the number of operations (mix of registers, cancellations and promotions).

| Implementation | Register | Promote next | Cancel (mid-queue) | m mixed operations |
|---|---|---|---|---|
| Sorted list (`bisect.insort`) | `O(n)` | `O(1)` | `O(n)` | `O(m·n)` |
| Naive: list + `sorted()` per promotion (`reference.py`) | `O(n log n)` | `O(n log n)` | `O(n log n)` | `O(m·n log n)` |
| `heapq` + `list.remove` + `heapify` | `O(log n)` | `O(log n)` | **`O(n)`** | `O(m·n)` worst case |
| Balanced BST / skip list | `O(log n)` | `O(log n)` | `O(log n)` | `O(m log n)` (higher constants, no stdlib support) |
| **Heap + lazy deletion (this repo)** | `O(log n)` | `O(log n)` | **`O(1)`** | **`O(m log n)`** |

The naive model is shipped in `reference.py` precisely so this table is not hypothetical: the
benchmark measures it at **76-132x slower** on realistic churn workloads.

---

## 4. Space

| Component | Space |
|---|---|
| Registrations | `O(n)` (never deleted — history is a feature) |
| Waitlist heap | `O(q + t)` — live entries plus tombstones |
| Seat pool | `O(c)` |
| Indexes | `O(n)` |
| Audit log | `O(n)` in memory (and on disk as JSONL) |
| **Total** | **`O(n + c)`** |

Canonical heap memory model: children of index `i` are `2i+1`, `2i+2`; parent is
`⌊(i-1)/2⌋`, so no pointers are stored — the array *is* the tree.

Tombstone debt is observable (`stats()["tombstone_debt"]`, `heap_array_size`) and reclaimed by
`LazyDeletionHeap.compact()` in a single `O(n)` pass, which rebuilds the array and
re-`heapify`s it. Recommended when `tombstone_debt` exceeds ~20% of live entries.

---

## 5. Rank queries: the honest trade-off

**Problem.** "You are number 47 in the queue" cannot be answered cheaply by a binary heap. The
underlying array is only *partially* ordered: a parent is smaller than its children, but the
heap does not encode "how many elements are smaller than X".

**Options evaluated**

| Option | Insert | Rank query | Extra space | Decision |
|---|---|---|---|---|
| Store a position with each entry | **`O(n)`** (every insert shifts ranks) | `O(1)` | `O(1)` | rejected: insert is the hot path |
| Drain a scratch copy of the heap until the key surfaces | `O(log n)` | `O(k log k)` | `O(q)` | rejected after measurement: 130 ms @ n=100k |
| **Counting scan** (count live entries with a smaller key) | `O(log n)` | **`O(n)`**, early-exit-free but allocation-free | `O(1)` | ✅ shipped: 6.4 ms @ n=100k |
| Fenwick tree over sequence numbers | `O(log n)` | `O(log n)` | `O(n)` | the right upgrade if rank becomes hot (needs a total order over a dense integer key) |
| Order-statistics balanced tree | `O(log n)` | `O(log n)` | `O(n)` | same, but Python has no stdlib implementation |

**Usage guidance that comes out of the measurement**

* A single `engine.waitlist()` call (`O(q log q)`) answers *all* positions at once — an export
  or an admin dashboard should always prefer it over n individual rank queries
  (`O(n·q)`, and it is the same information).
* "Your position when you joined" is free at arrival time, so we store
  `queue_size_at_registration` and never pay for a retroactive rank in the manifest.
* Live positions are exposed through a deliberately slow, honestly documented API:
  `waitlist_position()`.

---

## 6. Amortised and worst-case notes

* **`heapify` vs n pushes.** Building a heap from an existing list is `O(n)`, not
  `O(n log n)` — the engine exploits this when it builds the seat pool and when
  `LazyDeletionHeap.rebuild` reseeds a queue. (Measured: 47 ms vs 60 ms for 200k elements —
  a modest 1.3x, because pushing *ascending* values into a min-heap is `O(1)` amortised per
  item. The gap widens as input order becomes more adversarial, which is why the benchmark
  shuffles its input.)
* **Worst case is the average case here.** Every operation is bounded by `log c` or `log q`
  with no input-dependent branching (no quicksort-style degenerate case), so a hostile client
  cannot provoke super-linear behaviour — the only way to slow the engine down is to send more
  registrations.
* **GC/allocator pressure** dominates constants: the engine allocates one small dataclass per
  registration and one 2-element list per heap entry, and no temporary copies on the hot path
  (the rank scan and exports use the only two bulk copies in the design).

---

## 7. Reproducing the numbers

```bash
python -m seatalloc bench          # writes docs/BENCHMARKS.md + benchmarks/results.json
python -m pytest tests/test_complexity.py -q     # asserts the growth ratios
```

Methodology: fresh engine per measurement, repeated runs after a discarded warm-up pass, the
**minimum** reported (noise can only make a run slower, so the minimum is the least-biased
estimator). Ratios are asserted with generous bounds so slow CI machines do not produce false
failures, while an accidental `O(n)` hot path still fails loudly — which is exactly how the
`held_seats()` regression and the rank-query cost were discovered during development.
