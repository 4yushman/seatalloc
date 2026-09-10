"""A deliberately naive reference implementation, used as a test oracle.

The rules of the engine are simple enough to state in five lines:

    1. a request is CONFIRMED if a seat is free, otherwise WAITLISTED;
    2. the next attendee to be promoted is the smallest by
       ``(tier_weight, registered_at, arrival_sequence)``;
    3. a cancellation frees exactly one seat and triggers (at most) one promotion;
    4. capacity is never exceeded;
    5. promotion order is independent of arrival *order* of the calls — only of
       the recorded timestamps.

This module implements those rules the dumbest possible way: a Python list that
is fully re-sorted on every promotion (``O(n log n)`` per operation). It is
obviously correct by inspection, which is exactly what you want from an oracle.

Two independent uses:

* **Correctness oracle** — ``tests/test_stateful.py`` drives thousands of random
  operation sequences through both the heap engine and this model and asserts
  they agree seat-for-seat.
* **Performance baseline** — ``seatalloc.benchmarks`` measures the heap engine
  against this model so the ``O(log n)`` claims in the README are empirical,
  not decorative.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Attendee, EventConfig, RegistrationStatus

__all__ = ["NaiveAllocator", "NaiveRow"]


@dataclass(slots=True)
class NaiveRow:
    reg_id: str
    attendee_id: str
    name: str
    tier: str
    registered_at: float
    sequence: int
    status: RegistrationStatus = RegistrationStatus.WAITLISTED
    seat_number: int | None = None
    confirmed_at: float | None = None


@dataclass(slots=True)
class NaiveAllocator:
    """List-based, re-sort-everything allocator (the anti-pattern baseline)."""

    config: EventConfig
    rows: list[NaiveRow] = field(default_factory=list)
    _seq: int = 0

    # -- helpers ----------------------------------------------------------
    def _weight(self, tier: str) -> int:
        return self.config.tier_weight(tier)

    def _sort_key(self, row: NaiveRow) -> tuple[int, float, int]:
        return (self._weight(row.tier), row.registered_at, row.sequence)

    def _held(self) -> list[NaiveRow]:
        return [r for r in self.rows if r.status.holds_seat]

    def _used_seats(self) -> set[int]:
        return {r.seat_number for r in self._held() if r.seat_number is not None}

    def _next_free_seat(self) -> int:
        used = self._used_seats()
        for n in range(1, self.config.capacity + 1):
            if n not in used:
                return n
        raise AssertionError("no free seat although capacity is not exhausted")

    def queue(self) -> list[NaiveRow]:
        """Waitlisted rows in promotion order — the re-sort is the point."""
        waiting = [r for r in self.rows if r.status is RegistrationStatus.WAITLISTED]
        waiting.sort(key=self._sort_key)
        return waiting

    # -- operations -------------------------------------------------------
    def register(self, attendee: Attendee, at: float) -> NaiveRow:
        self._seq += 1
        row = NaiveRow(
            reg_id=f"{self.config.event_id}-R{self._seq:06d}",
            attendee_id=attendee.attendee_id,
            name=attendee.name,
            tier=attendee.tier,
            registered_at=at,
            sequence=self._seq,
        )
        self.rows.append(row)
        if len(self._held()) < self.config.capacity:
            row.status = RegistrationStatus.CONFIRMED
            row.seat_number = self._next_free_seat()
            row.confirmed_at = at
        else:
            row.status = RegistrationStatus.WAITLISTED
        return row

    def cancel(self, reg_id: str, at: float) -> bool:
        row = next(r for r in self.rows if r.reg_id == reg_id)
        if row.status.is_terminal:
            return False
        was_holding = row.status.holds_seat
        row.status = RegistrationStatus.CANCELLED
        if was_holding:
            row.seat_number = None
        self._promote(at)
        return True

    def _promote(self, at: float) -> list[str]:
        promoted: list[str] = []
        while len(self._held()) < self.config.capacity:
            waiting = self.queue()
            if not waiting:
                break
            nxt = waiting[0]
            nxt.status = RegistrationStatus.CONFIRMED
            nxt.seat_number = self._next_free_seat()
            nxt.confirmed_at = at
            promoted.append(nxt.reg_id)
        return promoted

    def set_capacity(self, new_capacity: int, at: float) -> int:
        if new_capacity < len(self._held()):
            raise ValueError("capacity below held seats")
        self.config.capacity = new_capacity
        return len(self._promote(at))

    # -- observation ------------------------------------------------------
    def confirmed(self) -> list[tuple[int, str]]:
        """``(seat_number, reg_id)`` pairs, seat-ordered — the manifest core."""
        pairs = [
            (r.seat_number, r.reg_id)
            for r in self._held()
            if r.status is RegistrationStatus.CONFIRMED and r.seat_number is not None
        ]
        pairs.sort()
        return pairs

    def status_of(self, reg_id: str) -> RegistrationStatus:
        return next(r for r in self.rows if r.reg_id == reg_id).status
