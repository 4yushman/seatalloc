# Design Notes

This document explains *why* the engine looks the way it does: the requirements it
was written against, the alternatives that were rejected, the failure modes it is
designed to survive, and the parts that are deliberately out of scope.

---

## 1. Requirements

### Functional

| ID | Requirement | Where it lives |
|---|---|---|
| F1 | Register an attendee; confirm if a seat is free, otherwise waitlist | `SeatAllocationEngine.register` |
| F2 | Allocate seats by registration timestamp, with configurable priority tiers | `EventConfig.tier_weights`, `_fill_open_seats` |
| F3 | A cancellation must immediately promote the next eligible attendee | `cancel` → `_fill_open_seats` |
| F4 | Enforce a hard seat cap; never oversell | `_free_seats` heap + I1 |
| F5 | Export the attendee manifest (and waitlist) to CSV | `csv_export.py` |
| F6 | Support "claim your seat within X minutes" promotions | `PromotionMode.OFFER` |
| F7 | Capacity may be raised (auto-promote) or lowered (never silently evict) | `set_capacity` |
| F8 | Every decision must be explainable after the fact | JSONL audit log + `replay()` |

### Non-functional

| ID | Requirement | How |
|---|---|---|
| N1 | `O(log n)` for register / cancel / promote | heaps + O(1) counters |
| N2 | Deterministic: same input → identical CSV, byte for byte | explicit timestamps, total ordering, injectable clock |
| N3 | Zero runtime dependencies | stdlib only |
| N4 | Verifiable | 99 tests incl. property + stateful + empirical complexity |
| N5 | Usable from a CLI, a library, or a cron job | `cli.py`, `__init__.py` |

---

## 2. The core model

A registration is a small state machine and is **never deleted**:

```
                     ┌─────────────┐   seat free    ┌───────────┐
   register ───────► │  (created)  ├───────────────►│ CONFIRMED │──► check_in ──► ATTENDED
                     └──────┬──────┘                └─────┬─────┘──► mark_no_show ─► NO_SHOW
                            │ event full                  │ cancel / decline
                            ▼                             ▼
                       ┌───────────┐   seat freed    ┌───────────┐
                       │ WAITLISTED├──(heap pop)────►│ CANCELLED │◄── EXPIRED (offer TTL)
                       └───────────┘                 └───────────┘
                              ▲
                              └── reinstate (keeps original arrival time)
```

**Why never delete?** The CSV audit export and the JSONL log must be able to answer
"what happened to this person?" — a deleted row cannot. Keeping history also makes
`reconcile()` possible: it can compare what *should* be true against what *is* true.

**Two state-occupancy predicates.** `occupies_seat` (CONFIRMED only) is for
"is this person a live claim on a seat right now", while `holds_seat`
(CONFIRMED, OFFERED, ATTENDED, NO_SHOW) is for pool accounting: a checked-in
attendee did occupy a seat and must not be considered free, otherwise a finished
event would promote someone off the waitlist and the seat-pool audit would drift.
This distinction was added after `reconcile()` flagged exactly that drift.

---

## 3. Data structure choices

### 3.1 The waitlist: a heap with lazy deletion

The queue needs (a) "who's next?" in `O(log n)` and (b) "remove this person from
the middle" — because people cancel *while queued*, which is the common case, not
an edge case.

`heapq` has no deletion primitive. The options:

| Approach | Delete cost | Notes |
|---|---|---|
| `list.remove` + `heapify` | `O(n)` | fine for one delete, quadratic across thousands |
| Rebuild the heap each time | `O(n)` | same problem, more code |
| Store `(deleted_flag, item)` and filter | `O(n)` | the filter *is* the cost, and stale entries still compare |
| **Lazy deletion with a side index** | **`O(1)`** | tombstone the payload; skip it when it surfaces |

We ship option 4. The implementation detail that makes it clean is that heap entries
are **mutable 2-slot lists** `[priority_tuple, item]`: tombstoning is `entry[1] = REMOVED`,
which needs no knowledge of where the entry currently lives. A `{key: entry}` index gives
`O(1)` membership checks and prevents double-insertion.

Consequences to be honest about:

* **Tombstone debt.** The array can be larger than the number of live entries. This is
  exposed as an observable metric (`stats()["tombstone_debt"]`) and reclaimed with a
  single `O(n)` `compact()` pass. Bounded by the number of cancellations, not by time.
* **No stable ordering in a heap.** Python's heap is not stable, so equal-priority entries
  could be served in any order. Fixed by making the key a *total* order:
  `(tier_weight, registered_at, arrival_sequence)`.

### 3.2 The seat pool: a second heap

Free seat numbers live in a min-heap, so a released seat is always reused lowest-first.
This yields tidy manifests (`1,2,3,7`) instead of growing numbers, and makes the recycled
seat predictable for the printed pass. Cost: `O(log c)` per allocate/release, `O(c)` to
build.

### 3.3 Running counters instead of scans

The original implementation computed `held_seats()` by scanning every registration. That
is `O(n)` and it sat directly on the `register` hot path, silently making the whole
allocator linear. It is now an incrementally maintained integer, updated in exactly one
place (`_transition`). Benefit beyond speed: there is a single choke point to reason about,
so a drifted counter is a local bug rather than an auditing problem.

Same reasoning for `_counts[status]` (O(1) status tallies) which replaced a scan in `stats()`.

### 3.4 Rank queries: deliberately not a heap primitive

"Where am I in the queue?" cannot be answered in `O(1)` by a binary heap; the array is only
partially ordered. Maintaining positions incrementally would cost `O(n)` **per insertion**,
which is the worst possible trade. The shipped approach is a `O(n)`, allocation-free counting
scan on demand, and the engine additionally stores `queue_size_at_registration` (free at
arrival time) so exports never need a rank at all. Full discussion and the `O(log n)`
upgrade path: [COMPLEXITY.md](COMPLEXITY.md#5-rank-queries-the-honest-trade-off).

---

## 4. Rules that were designed deliberately, not by accident

### 4.1 Promotion is atomic and immediate

`cancel`, `decline_offer` and `expire_offers` each end by calling `_fill_open_seats`, which
loops while a seat is free and the queue is non-empty. So the system cannot rest in a state
where a seat is free *and* someone is queued. That is asserted as a stateful invariant in
`tests/test_stateful.py::OfferModeStateMachine.no_seat_is_stranded`.

### 4.2 Offers are time-boxed, and expiry re-promotes in the same call

Real platforms give a promoted attendee a window to claim the seat; if they don't, the seat
"falls through" to the next person. Our `expire_offers()` does the sweep and the re-promotion
together, so a cron job cannot leave seats stranded between two sweeps. In AUTO mode the
promotion is direct — appropriate for free campus events where the friction of an accept
step costs more than it saves.

### 4.3 Capacity can grow, but shrinking never evicts

`set_capacity(n)` with `n < held_seats` raises `CapacityConflictError` instead of cancelling
people. A typo in an organiser's spreadsheet must not silently drop twenty confirmed
attendees; the caller has to cancel explicitly, which is auditable. Shrinking *above* the
held count is fine and reclaims the highest free seat numbers.

### 4.4 `cancel()` is idempotent

Returns `True` only when it changed something. Double-clicked buttons, retried webhooks and
duplicate admin actions are normal traffic; a second cancel must never promote a second
person. Property P3 asserts the state is unchanged after repeat cancellations.

### 4.5 Duplicates are blocked, but re-registration after cancelling is allowed

The duplicate guard looks at *active* registrations only: an attendee who cancelled and
changed their mind can register again, receiving a new `reg_id` (allocation history stays
intact) but losing their original queue seniority — except via the explicit `reinstate()`
admin path, which preserves it.

### 4.6 Seat numbers are stable, never renumbered

After churn, a manifest can read `seat 1, 3, 4`. That is intentional: an attendee's seat is
printed on their pass, and renumbering would silently change promises. Gaps are the honest
artifact of cancellations, and the seat-number pool always refills the lowest gap at the next
allocation.

### 4.7 Time is injected, never sampled

Every public method takes `at`. The default clock is used only by the CLI/demo. This is what
makes replay possible: the log stores each event's timestamp, and replay re-applies them in
order, reproducing the identical state (and the identical manifest SHA-256).

---

## 5. Failure modes and how the design responds

| Failure | Response |
|---|---|
| Duplicate submission (double-click, retry) | `DuplicateRegistrationError`, or `"duplicate"` from `try_register`; no state change |
| Registration outside the window | `EventWindowError` / `"window_closed"` |
| Spam / bot flood | `max_waitlist_size` bounds the queue; excess requests are recorded as `REJECTED` with reason `waitlist_full` (back-pressure, never silent loss) |
| Capacity typo downward | `CapacityConflictError`; nothing changes |
| Promoted attendee ignores the notification | offer TTL expires → seat recycled to the next person in the same sweep |
| Two cancellations of the same registration | second is a no-op; no double promotion |
| Stale heap entries after cancellations | skipped on pop (`REMOVED` sentinel); `compact()` reclaims space |
| Corrupt / truncated audit log | `read_events` reports the line number; `replay` refuses a log without an `event_created` header rather than silently reconstructing a wrong event |
| Manifest disputed | `manifest_fingerprint()` SHA-256 + `reconcile()` + replayable log |
| Counter drift (defensive) | `reconcile()` cross-checks counters against the actual registrations, seat pool and heap contents |

---

## 6. Testing philosophy

Three independent layers, because they catch different classes of bug:

1. **Example-based** — one test per business promise, named after the rule. Fast, readable,
   and the first thing a reviewer reads.
2. **Property-based (Hypothesis)** — invariants over hundreds of generated operation
   sequences. This is where "hostile input" lives: duplicate registrations, cancelling
   already-cancelled people, capacity raises mid-flight, identical timestamps.
3. **Model-based / stateful** — the fast engine and the deliberately naive list model
   (`reference.py`) run side by side; any divergence is a bug in one of them, and shrinking
   reduces the failure to the shortest reproducing sequence.

Plus **empirical complexity tests** that assert *growth ratios* rather than wall-clock
thresholds, so they survive noisy CI machines but still fail when a hot path degrades to
`O(n)`. That test is what caught the `held_seats()` regression, and the benchmark harness is
what exposed the `waitlist_position` cost.

The naive model earns its keep three times over: correctness oracle, benchmark baseline, and
documentation of the "obvious" design this repo deliberately did not ship.

---

## 7. Out of scope (and why)

* **Concurrency.** One engine instance is single-threaded and not thread-safe. Under a web
  server the correct pattern is a lock (or an atomic reservation in Redis/Postgres) at the
  API boundary; the allocator's determinism makes that layer easy to test.
* **Payments, refunds, ticket categories, venue seat maps.** Capacity is deliberately a
  single integer; multi-tier *ticket* capacity would mean one seat pool per ticket type.
* **Group bookings.** Would add a quantity dimension to both heaps (`4 seats together`), a
  genuinely different allocation problem.
* **Persistence beyond the log.** JSONL is the source of truth; a SQLite/Postgres store would
  be an index over it, not a replacement.
* **Notifications.** The engine emits events; sending WhatsApp/email is the caller's job.

## 8. Roadmap

1. FastAPI wrapper (`POST /events/{id}/register|cancel`, `GET /events/{id}/waitlist`) with the
   engine behind a lock, plus the manifest endpoint streaming from `export_manifest`.
2. **Priority aging**: after `T` hours in the queue, an attendee's effective weight improves,
   so a low tier can never be starved indefinitely ([standard FIFO-with-aging practice](https://www.workmate.com/blog/scaling-fifo-for-work-intake-design-first-in-first-out-queues-to-fairly-manage-requests)).
3. Order-statistics index (Fenwick tree over sequence numbers) for `O(log n)` rank queries.
4. No-show learning: estimate a per-event no-show rate from `mark_no_show` history and suggest
   an over-booking ratio, since free events commonly see 40-50% no-shows.
5. `compact()` scheduled as a maintenance task with tombstone-debt metrics exported to Prometheus.
