"""The allocation engine itself.

Design in one paragraph
-----------------------
Registration requests arrive as an unordered stream. Each request is stamped
with an arrival time and an arrival sequence, pushed into a **binary min-heap**
keyed on ``(tier_weight, registered_at, sequence)``, and either confirmed
immediately (when a seat is free) or parked on the heap as ``WAITLISTED``.
When a seat frees — a cancellation, a declined/expired offer, or an increased
capacity — the engine pops the heap root and promotes that attendee, so the
"next in line" decision is ``O(log n)`` and provably fair: it is a total order,
not a heuristic.

Key correctness invariants (enforced and property-tested)::

    I1.  held_seats = |CONFIRMED| + |OFFERED|  <=  capacity        (never oversell)
    I2.  every CONFIRMED/OFFERED row has 1 <= seat_number <= capacity
    I3.  seat numbers are unique across held seats
    I4.  the waitlist heap contains exactly the WAITLISTED registrations
    I5.  promotion order == sorted order by (tier_weight, registered_at, sequence)
    I6.  every registration id is unique and never reused
"""

from __future__ import annotations

import heapq
import itertools
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any

from .models import (
    Attendee,
    CapacityConflictError,
    DuplicateRegistrationError,
    EventConfig,
    EventWindowError,
    PromotionMode,
    Registration,
    RegistrationStatus,
    StatusConflictError,
    UnknownRegistrationError,
    WaitlistFullError,
)
from .priority import LazyDeletionHeap

__all__ = ["SeatAllocationEngine", "Clock"]

Clock = Callable[[], float]


class SeatAllocationEngine:
    """Waitlist + seat-cap manager for a single event.

    The engine is **deterministic**: given the same sequence of calls with the
    same explicit timestamps it always produces the same state and the same CSV
    manifest. That property is what makes replay and property-based testing
    possible, and it is tested directly (see ``tests/test_replay.py``).

    Parameters
    ----------
    config:
        The event's static rules (:class:`~seatalloc.models.EventConfig`).
    clock:
        Injectable time source (defaults to :func:`time.time`). Every public
        method also accepts an explicit ``at`` argument which overrides it —
        always pass ``at`` in tests, simulations and replays.
    audit_hook:
        Optional callback invoked with one dict per state change. This is how
        the JSONL audit log and the SQLite-free replay mechanism are wired in,
        without the engine depending on any I/O module.
    """

    __slots__ = (
        "config",
        "_clock",
        "_audit_hook",
        "_by_id",
        "_by_attendee",
        "_waitlist",
        "_free_seats",
        "_sequence",
        "_audit",
        "_promotions",
        "_expirations",
        "_held_count",
        "_counts",
    )

    def __init__(
        self,
        config: EventConfig,
        *,
        clock: Clock | None = None,
        audit_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self._clock: Clock = clock or time.time
        self._audit_hook = audit_hook

        self._by_id: dict[str, Registration] = {}
        self._by_attendee: dict[str, str] = {}
        self._waitlist: LazyDeletionHeap[Registration] = LazyDeletionHeap(
            key_fn=lambda r: (config.tier_weight(r.attendee.tier), r.registered_at, r.sequence),
            id_fn=lambda r: r.reg_id,
        )
        # Free seat numbers live in a min-heap so recycled seats are handed out
        # lowest-first, which keeps manifests tidy and stable.
        self._free_seats: list[int] = list(range(1, config.capacity + 1))
        heapq.heapify(self._free_seats)
        self._sequence = itertools.count(1)
        self._audit: list[dict[str, Any]] = []
        self._promotions = 0
        self._expirations = 0
        # Running counters. These exist purely so that the hot paths -- e.g.
        # "is a seat free?" on every single registration -- are O(1) instead of
        # rescanning every registration. (A scan here silently turns the whole
        # allocator into O(n) per request; the empirical tests in
        # tests/test_complexity.py are what catch that regression.)
        self._held_count = 0
        self._counts: dict[RegistrationStatus, int] = dict.fromkeys(RegistrationStatus, 0)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def capacity(self) -> int:
        return self.config.capacity

    @property
    def registrations(self) -> Sequence[Registration]:
        """All registrations ever created, in creation order."""
        return list(self._by_id.values())

    @property
    def audit_trail(self) -> Sequence[dict[str, Any]]:
        """In-memory copy of every emitted audit event."""
        return list(self._audit)

    def get(self, reg_id: str) -> Registration:
        """Return the registration or raise :class:`UnknownRegistrationError`."""
        try:
            return self._by_id[reg_id]
        except KeyError:
            raise UnknownRegistrationError(reg_id) from None

    def status_of(self, reg_id: str) -> RegistrationStatus:
        return self.get(reg_id).status

    def held_seats(self) -> int:
        """Seats currently allocated, maintained incrementally in O(1).

        Counts every registration that ``RegistrationStatus.holds_seat`` --
        confirmed, offered, and (post-event) attended / no-show.
        """
        return self._held_count

    def open_seats(self) -> int:
        """Capacity minus held seats; never negative."""
        return max(0, self.capacity - self.held_seats())

    def waitlist_size(self) -> int:
        return len(self._waitlist)

    def confirmed(self) -> list[Registration]:
        """Confirmed attendees ordered by seat number (manifest order)."""
        rows = [r for r in self._by_id.values() if r.status is RegistrationStatus.CONFIRMED]
        rows.sort(key=lambda r: (r.seat_number is None, r.seat_number))
        return rows

    def confirmed_seats(self) -> list[tuple[int, str]]:
        """``(seat_number, reg_id)`` pairs, seat-ordered.

        This is the allocator's *observable output*: two implementations that
        agree on this list have made identical allocation decisions, which is
        exactly what the oracle tests compare.
        """
        return [
            (r.seat_number, r.reg_id)
            for r in self.confirmed()
            if r.seat_number is not None
        ]

    def offers(self) -> list[Registration]:
        """Outstanding promotions awaiting an accept/decline decision."""
        rows = [r for r in self._by_id.values() if r.status is RegistrationStatus.OFFERED]
        rows.sort(key=lambda r: (r.offer_expires_at or 0.0, r.reg_id))
        return rows

    def waitlist(self) -> list[Registration]:
        """The queue in promotion order, i.e. exactly who is next.

        ``O(n log n)``: a heap gives the next element in ``O(log n)`` but a full
        *ordering* in ``O(n log n)``. Exports and UI need the ordering, so we pay
        for it once here instead of on every query.
        """
        return self._waitlist.ordered_items()

    def waitlist_position(self, reg_id: str) -> int | None:
        """1-based position in the queue, or ``None`` if not waiting.

        Deliberately **not** maintained incrementally: a binary heap cannot
        report rank in O(1), and maintaining ranks by hand would cost O(n) per
        *insertion* — the exact thing this design avoids. Instead the rank is
        computed on demand with a single ``O(n)`` counting scan, which is the
        right trade for a "where am I in the queue?" API call that happens a few
        times a second rather than on every one of thousands of registrations.
        ``docs/COMPLEXITY.md`` documents the ``O(log n)`` alternatives.
        """
        reg = self._by_id.get(reg_id)
        if reg is None or reg.status is not RegistrationStatus.WAITLISTED:
            return None
        return self._waitlist.position_of(reg_id)

    def stats(self, *, at: float | None = None) -> dict[str, Any]:
        """Operational snapshot — the numbers an organiser actually asks for."""
        now = self._now(at)
        # O(1) status tallies from the running counters; only the wait-time
        # statistics below require a scan, and this is an analytics call.
        by_status: dict[str, int] = {s.value: n for s, n in self._counts.items()}
        confirmed = [r for r in self._by_id.values() if r.confirmed_at is not None]
        waits = [r.wait_time_seconds(now) for r in confirmed]
        waited = [r for r in self._by_id.values() if r.status is RegistrationStatus.WAITLISTED]
        waiting_times = [now - r.registered_at for r in waited]
        return {
            "event_id": self.config.event_id,
            "capacity": self.capacity,
            "held_seats": self.held_seats(),
            "confirmed": by_status[RegistrationStatus.CONFIRMED.value],
            "offers_outstanding": by_status[RegistrationStatus.OFFERED.value],
            "waitlisted": by_status[RegistrationStatus.WAITLISTED.value],
            "cancelled": by_status[RegistrationStatus.CANCELLED.value],
            "rejected": by_status[RegistrationStatus.REJECTED.value],
            "expired": by_status[RegistrationStatus.EXPIRED.value],
            "attended": by_status[RegistrationStatus.ATTENDED.value],
            "no_show": by_status[RegistrationStatus.NO_SHOW.value],
            "open_seats": self.open_seats(),
            "fill_rate": round(self.held_seats() / self.capacity, 4) if self.capacity else 0.0,
            "total_registrations": len(self._by_id),
            "total_promotions": self._promotions,
            "expired_offers": self._expirations,
            "avg_wait_to_confirm_seconds": round(sum(waits) / len(waits), 2) if waits else 0.0,
            "max_wait_to_confirm_seconds": round(max(waits), 2) if waits else 0.0,
            "avg_current_wait_seconds": round(sum(waiting_times) / len(waiting_times), 2)
            if waiting_times
            else 0.0,
            "tombstone_debt": self._waitlist.stale_entries,
            "heap_array_size": self._waitlist.heap_size,
        }

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(
        self,
        attendee: Attendee,
        *,
        registered_at: float | None = None,
        note: str = "",
    ) -> Registration:
        """Register one attendee.

        Returns a ``CONFIRMED`` registration when a seat is available, otherwise
        a ``WAITLISTED`` one holding its place on the heap.

        Raises
        ------
        DuplicateRegistrationError
            The attendee already has an active (non-cancelled) registration.
        EventWindowError
            ``registered_at`` falls outside the configured registration window.
        WaitlistFullError
            The bounded waitlist is full.
        """
        at = self._now(registered_at)
        self._check_window(at)

        existing_id = self._by_attendee.get(attendee.attendee_id)
        if existing_id is not None:
            existing = self._by_id[existing_id]
            if existing.status is not RegistrationStatus.CANCELLED:
                raise DuplicateRegistrationError(attendee.attendee_id, existing_id)

        if self.open_seats() == 0 and self._waitlist_full():
            reg = self._create(attendee, at, note=note)
            self._reject(reg, at, reason="waitlist_full")
            raise WaitlistFullError(
                f"waitlist is full ({self.config.max_waitlist_size}) for event "
                f"{self.config.event_id!r}"
            )

        reg = self._create(attendee, at, note=note)
        decision = "CONFIRMED" if self.open_seats() > 0 else "WAITLISTED"
        # The "register" event is the *primary* audit record: it carries the full
        # attendee payload, which is exactly what the replayer needs to re-create
        # this registration later (see seatalloc.persistence.replay).
        self._emit(
            "register",
            at,
            reg_id=reg.reg_id,
            decision=decision,
            status=decision,
            attendee_id=attendee.attendee_id,
            name=attendee.name,
            email=attendee.email,
            tier=attendee.tier,
            registered_at=at,
        )
        if decision == "CONFIRMED":
            self._hold_seat(reg, at, from_waitlist=False)
        else:
            self._waitlist.push(reg)
            # O(1): we record the *queue depth* this attendee joined into, not
            # their exact rank. Exact rank needs an order-statistics structure
            # (see docs/COMPLEXITY.md); the depth is what the queue-position
            # "you are number N" email actually needs, and it is free.
            size = len(self._waitlist)
            reg.queue_size_at_registration = size
            self._emit("waitlist_push", at, reg_id=reg.reg_id, queue_size=size)
        return reg

    def try_register(
        self,
        attendee: Attendee,
        *,
        registered_at: float | None = None,
        note: str = "",
    ) -> tuple[Registration | None, str | None]:
        """Non-raising variant of :meth:`register` for bulk/stream ingestion.

        Returns ``(registration, None)`` on success, or ``(None, reason)`` where
        ``reason`` is one of ``"duplicate"``, ``"window_closed"``,
        ``"waitlist_full"``. Ideal for importing a CSV of registrations where
        bad rows must not abort the whole batch.
        """
        at = self._now(registered_at)

        existing_id = self._by_attendee.get(attendee.attendee_id)
        if existing_id is not None and self._by_id[existing_id].status is not RegistrationStatus.CANCELLED:
            return None, "duplicate"
        try:
            self._check_window(at)
        except EventWindowError:
            return None, "window_closed"

        if self.open_seats() == 0 and self._waitlist_full():
            reg = self._create(attendee, at, note=note)
            self._reject(reg, at, reason="waitlist_full")
            return None, "waitlist_full"
        return self.register(attendee, registered_at=at, note=note), None

    def register_many(
        self,
        attendees: Iterable[Attendee],
        *,
        registered_at: float | None = None,
        spacing: float = 0.0,
    ) -> list[Registration]:
        """Bulk-ingest attendees in one call.

        ``spacing`` staggers synthetic timestamps by N seconds per attendee so
        bulk imports keep a deterministic arrival order (real ingress would use
        the actual submission time of each row).
        """
        base = self._now(registered_at)
        out: list[Registration] = []
        for i, attendee in enumerate(attendees):
            out.append(self.register(attendee, registered_at=base + i * spacing))
        return out

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------
    def cancel(self, reg_id: str, *, at: float | None = None, reason: str = "attendee_cancelled") -> bool:
        """Withdraw a registration and recycle the seat (idempotent).

        Returns ``True`` if this call changed the state, ``False`` if the
        registration was already terminal — so a double-clicked "Cancel" button
        cannot double-promote somebody from the waitlist.
        """
        now = self._now(at)
        reg = self.get(reg_id)
        if reg.status.is_terminal:
            self._emit("cancel_noop", now, reg_id=reg_id, status=reg.status.value)
            return False

        previous = reg.status
        was_holding = reg.status.holds_seat
        if was_holding:
            self._release_seat(reg)
        reg.offer_expires_at = None

        # O(1) tombstone if present; harmless no-op for OFFERED/CONFIRMED rows,
        # which are never in the queue in the first place.
        self._waitlist.remove(reg_id)

        self._transition(reg, RegistrationStatus.CANCELLED)
        reg.decided_at = now
        reg.note = reason
        self._emit("cancel", now, reg_id=reg_id, previous=previous.value, reason=reason,
                   seat_recycled=was_holding)
        self._fill_open_seats(now)
        return True

    def accept_offer(self, reg_id: str, *, at: float | None = None) -> Registration:
        """Convert an ``OFFERED`` promotion into a firm ``CONFIRMED`` seat."""
        now = self._now(at)
        reg = self.get(reg_id)
        if reg.status is not RegistrationStatus.OFFERED:
            raise StatusConflictError(reg_id, reg.status.value, "accept_offer")
        self._transition(reg, RegistrationStatus.CONFIRMED)
        reg.confirmed_at = now
        reg.decided_at = now
        reg.offer_expires_at = None
        self._emit("accept_offer", now, reg_id=reg_id, seat_number=reg.seat_number)
        return reg

    def decline_offer(self, reg_id: str, *, at: float | None = None, reason: str = "declined") -> bool:
        """Give up a promotion; the seat goes to the next eligible attendee."""
        now = self._now(at)
        reg = self.get(reg_id)
        if reg.status is not RegistrationStatus.OFFERED:
            raise StatusConflictError(reg_id, reg.status.value, "decline_offer")
        self._release_seat(reg)
        self._transition(reg, RegistrationStatus.CANCELLED)
        reg.decided_at = now
        reg.note = reason
        self._emit("decline_offer", now, reg_id=reg_id, reason=reason)
        self._fill_open_seats(now)
        return True

    def expire_offers(self, *, at: float | None = None) -> list[str]:
        """Sweep promotions past their acceptance window (the 'offer TTL' job).

        Expired promotions are marked ``EXPIRED`` and their seats are handed to
        the next attendee in the same call, so a seat is never stranded while
        the queue is non-empty.
        """
        now = self._now(at)
        expired: list[str] = []
        self._emit("expire_offers", now, candidates=len(self.offers()))
        for reg in self.offers():
            if reg.offer_expires_at is not None and reg.offer_expires_at <= now:
                self._release_seat(reg)
                self._transition(reg, RegistrationStatus.EXPIRED)
                reg.decided_at = now
                self._emit("expire_offer", now, reg_id=reg.reg_id, seat_number=reg.seat_number)
                expired.append(reg.reg_id)
        if expired:
            self._expirations += len(expired)
            self._fill_open_seats(now)
        return expired

    def reinstate(self, reg_id: str, *, at: float | None = None) -> Registration:
        """Undo a cancellation (organiser goodwill, admin appeal).

        The original arrival timestamp is preserved so the attendee keeps their
        earned place in line; the sequence number ensures ties still resolve
        deterministically against later arrivals.
        """
        now = self._now(at)
        reg = self.get(reg_id)
        if reg.status is not RegistrationStatus.CANCELLED:
            raise StatusConflictError(reg_id, reg.status.value, "reinstate")
        reg.decided_at = None
        reg.note = "reinstated"
        self._by_attendee[reg.attendee.attendee_id] = reg.reg_id
        if self.open_seats() > 0:
            self._hold_seat(reg, now, from_waitlist=True)
        else:
            self._transition(reg, RegistrationStatus.WAITLISTED)
            self._waitlist.push(reg)
            self._emit("reinstate", now, reg_id=reg_id, status=reg.status.value,
                       queue_size=len(self._waitlist))
        return reg

    def check_in(self, reg_id: str, *, at: float | None = None) -> Registration:
        """Post-event door scan: CONFIRMED -> ATTENDED."""
        now = self._now(at)
        reg = self.get(reg_id)
        if reg.status is not RegistrationStatus.CONFIRMED:
            raise StatusConflictError(reg_id, reg.status.value, "check_in")
        self._transition(reg, RegistrationStatus.ATTENDED)
        reg.decided_at = now
        self._emit("check_in", now, reg_id=reg_id, seat_number=reg.seat_number)
        return reg

    def mark_no_show(self, reg_id: str, *, at: float | None = None) -> Registration:
        """Post-event: CONFIRMED -> NO_SHOW (analytics + over-booking calibration)."""
        now = self._now(at)
        reg = self.get(reg_id)
        if reg.status is not RegistrationStatus.CONFIRMED:
            raise StatusConflictError(reg_id, reg.status.value, "mark_no_show")
        self._transition(reg, RegistrationStatus.NO_SHOW)
        reg.decided_at = now
        self._emit("mark_no_show", now, reg_id=reg_id, seat_number=reg.seat_number)
        return reg

    def set_capacity(self, new_capacity: int, *, at: float | None = None) -> int:
        """Resize the event.

        Increasing capacity immediately pulls people off the waitlist.
        *Decreasing* below the number of held seats raises
        :class:`CapacityConflictError` rather than silently cancelling confirmed
        attendees — the caller must explicitly cancel first, so nobody loses a
        seat as a side effect of an organiser's typo.

        Returns the number of attendees promoted as a result.
        """
        if new_capacity < 0:
            raise ValueError("capacity must be >= 0")
        now = self._now(at)
        held = self.held_seats()
        if new_capacity < held:
            raise CapacityConflictError(
                f"cannot shrink capacity to {new_capacity}: {held} seats are held "
                f"(confirmations + live offers). Cancel or decline them first."
            )
        old = self.config.capacity
        if new_capacity == old:
            return 0
        self.config.capacity = new_capacity
        if new_capacity > old:
            for n in range(old + 1, new_capacity + 1):
                heapq.heappush(self._free_seats, n)
        else:  # shrink but still >= held: reclaim the highest free numbers
            free = set(range(new_capacity + 1, old + 1))
            self._free_seats = [n for n in self._free_seats if n not in free]
            heapq.heapify(self._free_seats)
        self._emit("set_capacity", now, old=old, new=new_capacity)
        promoted = self._fill_open_seats(now)
        return len(promoted)

    # ------------------------------------------------------------------
    # Integrity / reconciliation
    # ------------------------------------------------------------------
    def reconcile(self) -> list[str]:
        """Self-audit. Returns a list of invariant violations (empty == healthy).

        This is the "an event organiser's export and the counter disagree"
        scenario made cheap to detect: run it after any bulk import, before
        publishing a manifest, or in CI.
        """
        problems: list[str] = []
        held = [r for r in self._by_id.values() if r.status.holds_seat]

        if len(held) > self.capacity:
            problems.append(f"I1 overbooked: {len(held)} held > capacity {self.capacity}")

        seats: dict[int, str] = {}
        for reg in held:
            if reg.seat_number is None or not (1 <= reg.seat_number <= self.capacity):
                problems.append(f"I2 bad seat number {reg.seat_number!r} for {reg.reg_id}")
                continue
            if reg.seat_number in seats:
                problems.append(
                    f"I3 duplicate seat {reg.seat_number} held by {seats[reg.seat_number]} "
                    f"and {reg.reg_id}"
                )
            seats[reg.seat_number] = reg.reg_id

        waiting = {r.reg_id for r in self._by_id.values() if r.status is RegistrationStatus.WAITLISTED}
        queued = {r.reg_id for r in self._waitlist.ordered_items()}
        if waiting != queued:
            problems.append(
                f"I4 heap/status mismatch: only-in-status={sorted(waiting - queued)[:5]} "
                f"only-in-heap={sorted(queued - waiting)[:5]}"
            )

        order = [r.reg_id for r in self.waitlist()]
        keys = {
            r.reg_id: (self.config.tier_weight(r.tier), r.registered_at, r.sequence)
            for r in self._by_id.values()
        }
        if order != sorted(order, key=lambda rid: keys[rid]):
            problems.append("I5 heap order is not sorted by (tier_weight, registered_at, sequence)")

        active_by_attendee: dict[str, str] = {}
        for reg in self._by_id.values():
            if reg.status.is_terminal:
                continue
            clash = active_by_attendee.get(reg.attendee.attendee_id)
            if clash is not None:
                problems.append(
                    f"I6 attendee {reg.attendee.attendee_id!r} has two active "
                    f"registrations: {clash} and {reg.reg_id}"
                )
            active_by_attendee[reg.attendee.attendee_id] = reg.reg_id

        free_expected = set(range(1, self.capacity + 1)) - set(seats)
        free_actual = set(self._free_seats)
        if free_expected != free_actual:
            problems.append(
                f"seat-number pool drift: expected {sorted(free_expected)[:5]} got "
                f"{sorted(free_actual)[:5]}"
            )
        return problems

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _now(self, at: float | None) -> float:
        return float(self._clock() if at is None else at)

    def _check_window(self, at: float) -> None:
        cfg = self.config
        if cfg.registration_opens_at is not None and at < cfg.registration_opens_at:
            raise EventWindowError(
                f"registration opens at {cfg.registration_opens_at}; request at {at}"
            )
        if cfg.registration_closes_at is not None and at > cfg.registration_closes_at:
            raise EventWindowError(
                f"registration closed at {cfg.registration_closes_at}; request at {at}"
            )

    def _waitlist_full(self) -> bool:
        limit = self.config.max_waitlist_size
        return limit is not None and len(self._waitlist) >= limit

    def _create(self, attendee: Attendee, at: float, *, note: str) -> Registration:
        """Stamp a new registration with an arrival time + unique sequence."""
        seq = next(self._sequence)
        reg_id = f"{self.config.event_id}-R{seq:06d}"
        reg = Registration(
            reg_id=reg_id,
            attendee=attendee,
            registered_at=at,
            sequence=seq,
            note=note,
        )
        self._by_id[reg_id] = reg
        self._by_attendee[attendee.attendee_id] = reg_id
        self._init_status(reg)
        return reg

    def _transition(self, reg: Registration, new_status: RegistrationStatus) -> None:
        """Single choke point for status changes — keeps the counters honest.

        Every mutation of ``reg.status`` in this module goes through here, which
        is why ``held_seats()`` can be O(1) and why a drifted counter is a bug we
        can reason about locally instead of auditing every call site.
        """
        old = reg.status
        if old is new_status:
            return
        reg.status = new_status
        self._counts[old] -= 1
        self._counts[new_status] += 1
        if old.holds_seat != new_status.holds_seat:
            self._held_count += 1 if new_status.holds_seat else -1

    def _init_status(self, reg: Registration) -> None:
        """Count the status a freshly created registration starts in."""
        self._counts[reg.status] += 1
        if reg.status.holds_seat:  # pragma: no cover - new rows start WAITLISTED
            self._held_count += 1

    def _reject(self, reg: Registration, at: float, *, reason: str) -> None:
        self._transition(reg, RegistrationStatus.REJECTED)
        reg.decided_at = at
        reg.note = reason
        self._emit(
            "reject",
            at,
            reg_id=reg.reg_id,
            status=RegistrationStatus.REJECTED.value,
            reason=reason,
            attendee_id=reg.attendee.attendee_id,
            name=reg.attendee.name,
            email=reg.attendee.email,
            tier=reg.attendee.tier,
            registered_at=at,
        )

    def _hold_seat(self, reg: Registration, at: float, *, from_waitlist: bool) -> None:
        """Assign a seat number and move the registration into a *held* status.

        ``from_waitlist`` distinguishes a genuine **promotion** (waitlist ->
        seat, counted in ``total_promotions`` and eligible for the time-boxed
        offer flow) from a **direct** registration, which is always CONFIRMED
        outright: an attendee who registers while a seat is free should never be
        asked to "accept" a seat they just asked for.
        """
        if not self._free_seats:  # pragma: no cover - guarded by I1
            raise CapacityConflictError(
                f"no free seat number for {reg.reg_id} although capacity {self.capacity} "
                f"is not exhausted — seat-number bookkeeping is inconsistent"
            )
        reg.seat_number = heapq.heappop(self._free_seats)
        if from_waitlist:
            reg.promoted_from_waitlist = True
        if from_waitlist and self.config.promotion_mode is PromotionMode.OFFER:
            self._transition(reg, RegistrationStatus.OFFERED)
            reg.offered_at = at
            reg.offer_expires_at = at + self.config.offer_ttl_seconds
            self._promotions += 1
            self._emit("offer_seat", at, reg_id=reg.reg_id, seat_number=reg.seat_number,
                       expires_at=reg.offer_expires_at)
        else:
            self._transition(reg, RegistrationStatus.CONFIRMED)
            reg.confirmed_at = at
            if from_waitlist:
                self._promotions += 1
            self._emit("confirm_seat", at, reg_id=reg.reg_id, seat_number=reg.seat_number)

    def _release_seat(self, reg: Registration) -> None:
        if reg.seat_number is not None:
            heapq.heappush(self._free_seats, reg.seat_number)
            reg.seat_number = None

    def _fill_open_seats(self, at: float) -> list[str]:
        """Promote from the heap until there are no free seats (or queue is empty)."""
        promoted: list[str] = []
        while self.open_seats() > 0:
            nxt = self._waitlist.peek()
            if nxt is None:
                break
            self._waitlist.pop()
            if nxt.status is not RegistrationStatus.WAITLISTED:
                continue  # defensive: a stale entry slipped through
            self._hold_seat(nxt, at, from_waitlist=True)
            promoted.append(nxt.reg_id)
            # In OFFER mode each promotion *holds* its seat until the TTL
            # expires, so ``open_seats()`` falls by one per iteration and the
            # loop terminates on its own once every free seat has been offered.
        return promoted

    def _emit(self, op: str, at: float, **fields: Any) -> None:
        event = {"op": op, "at": at, "sequence": len(self._audit), **fields}
        if "status" not in event and fields.get("reg_id") in self._by_id:
            event["status"] = self._by_id[fields["reg_id"]].status.value
        self._audit.append(event)
        if self._audit_hook is not None:
            self._audit_hook(event)

    # ------------------------------------------------------------------
    # Bulk helpers used by the CLI / benchmarks
    # ------------------------------------------------------------------
    def iter_held(self) -> Iterator[Registration]:
        return (r for r in self._by_id.values() if r.status.holds_seat)

    def snapshot(self) -> dict[str, Any]:
        """Full serialisable state — used by the replay-equivalence test."""
        return {
            "config": self.config.to_dict(),
            "promotions": self._promotions,
            "expirations": self._expirations,
            "registrations": [r.to_dict() for r in self._by_id.values()],
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"SeatAllocationEngine(event={self.config.event_id!r}, capacity={self.capacity}, "
            f"held={self.held_seats()}, waitlist={self.waitlist_size()})"
        )
