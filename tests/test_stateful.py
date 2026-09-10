"""Stateful / model-based testing.

Some bugs only appear in a *sequence* of operations — a cancellation that leaves
a tombstone the promotion loop later trips over, a capacity raise that promotes
one person too many, a reinstate that resurrects a stale queue entry. Hypothesis'
``RuleBasedStateMachine`` generates long random interleavings of our rules and
checks the invariants after **every** step, shrinking any failure to the shortest
reproducing sequence.

Crucially, the machine runs two independent implementations side by side:

* ``self.engine`` — the heap-based engine under test;
* ``self.model``  — the naive list model from ``seatalloc.reference``.

Any disagreement is a bug in one of them, and the shrinking makes it obvious which.
"""

from __future__ import annotations

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from seatalloc import Attendee, EventConfig, PromotionMode, SeatAllocationEngine
from seatalloc.reference import NaiveAllocator

TIERS = ["GENERAL", "MEMBER", "VOLUNTEER", "ORGANIZER"]
CAPACITY = 4


class AllocationStateMachine(RuleBasedStateMachine):
    """Interleave registration, cancellation and capacity changes."""

    def __init__(self) -> None:
        super().__init__()
        config = EventConfig(event_id="SM", name="State Machine", capacity=CAPACITY)
        self.config = config
        self.engine = SeatAllocationEngine(EventConfig.from_dict(config.to_dict()))
        self.model = NaiveAllocator(EventConfig.from_dict(config.to_dict()))
        self.clock = 1_700_000_000.0
        self.people: list[Attendee] = []
        self.active: dict[str, str] = {}   # attendee_id -> engine reg_id
        self.known: list[str] = []         # reg_ids we believe exist

    # -- helpers ---------------------------------------------------------
    def _tick(self, step: float = 1.0) -> float:
        self.clock += step
        return self.clock

    # -- rules -----------------------------------------------------------
    @rule(index=st.integers(min_value=0, max_value=14), tier=st.sampled_from(TIERS))
    def register(self, index: int, tier: str) -> None:
        """Register a person; duplicates must be rejected identically."""
        person = Attendee(f"P{index:02d}", f"Person {index}", f"p{index}@example.com", tier)
        already = person.attendee_id in self.active

        if already:
            from seatalloc import DuplicateRegistrationError

            with pytest.raises(DuplicateRegistrationError):
                self.engine.register(person, registered_at=self._tick())
            return

        engine_reg = self.engine.register(person, registered_at=self._tick())
        model_row = self.model.register(person, engine_reg.registered_at)
        self.active[person.attendee_id] = engine_reg.reg_id
        self.known.append(engine_reg.reg_id)
        assert engine_reg.reg_id == model_row.reg_id, "id assignment diverged"

    @rule(data=st.data())
    @precondition(lambda self: bool(self.known))
    def cancel(self, data: st.DataObject) -> None:
        """Cancel a random registration (including already-cancelled ones)."""
        reg_id = data.draw(st.sampled_from(self.known))
        at = self._tick()

        engine_changed = self.engine.cancel(reg_id, at=at)
        model_changed = self.model.cancel(reg_id, at=at)
        assert engine_changed == model_changed

        if engine_changed:
            reg = self.engine.get(reg_id)
            self.active.pop(reg.attendee.attendee_id, None)

    @rule(delta=st.integers(min_value=1, max_value=3))
    @precondition(lambda self: True)
    def grow_capacity(self, delta: int) -> None:
        at = self._tick()
        self.engine.set_capacity(self.engine.capacity + delta, at=at)
        self.model.set_capacity(self.model.config.capacity + delta, at)

    @rule(ticks=st.integers(min_value=0, max_value=500))
    def advance_time(self, ticks: int) -> None:
        self._tick(float(ticks))

    # -- invariants (checked after every single step) ---------------------
    @invariant()
    def allocation_matches_model(self) -> None:
        assert self.engine.confirmed_seats() == self.model.confirmed()

    @invariant()
    def queue_matches_model(self) -> None:
        assert [r.reg_id for r in self.engine.waitlist()] == [
            r.reg_id for r in self.model.queue()
        ]

    @invariant()
    def capacity_is_respected(self) -> None:
        assert self.engine.held_seats() <= self.engine.capacity
        assert self.engine.open_seats() >= 0
        assert self.engine.reconcile() == []


TestAllocationStateMachine = AllocationStateMachine.TestCase
TestAllocationStateMachine.settings = settings(
    max_examples=40, stateful_step_count=25, deadline=None
)


class OfferModeStateMachine(RuleBasedStateMachine):
    """OFFER mode: seats must never be stranded while the queue is non-empty."""

    def __init__(self) -> None:
        super().__init__()
        self.engine = SeatAllocationEngine(
            EventConfig(
                event_id="OFFER",
                name="Offer mode",
                capacity=2,
                promotion_mode=PromotionMode.OFFER,
                offer_ttl_seconds=100.0,
            )
        )
        self.clock = 1_700_000_000.0
        self.known: list[str] = []
        self.counter = 0

    def _tick(self, step: float = 1.0) -> float:
        self.clock += step
        return self.clock

    @rule(tier=st.sampled_from(TIERS))
    def register(self, tier: str) -> None:
        self.counter += 1
        person = Attendee(f"P{self.counter:03d}", f"P {self.counter}", f"p{self.counter}@e.com", tier)
        reg = self.engine.register(person, registered_at=self._tick())
        self.known.append(reg.reg_id)

    @rule(data=st.data())
    @precondition(lambda self: bool(self.known))
    def cancel(self, data: st.DataObject) -> None:
        reg_id = data.draw(st.sampled_from(self.known))
        self.engine.cancel(reg_id, at=self._tick())

    @rule(data=st.data())
    @precondition(lambda self: bool(self.engine.offers()))
    def accept_offer(self, data: st.DataObject) -> None:
        reg_id = data.draw(st.sampled_from([r.reg_id for r in self.engine.offers()]))
        self.engine.accept_offer(reg_id, at=self._tick())

    @rule(data=st.data())
    @precondition(lambda self: bool(self.engine.offers()))
    def decline_offer(self, data: st.DataObject) -> None:
        reg_id = data.draw(st.sampled_from([r.reg_id for r in self.engine.offers()]))
        self.engine.decline_offer(reg_id, at=self._tick())

    @rule(ticks=st.integers(min_value=1, max_value=400))
    def advance_time_and_expire(self, ticks: int) -> None:
        self.engine.expire_offers(at=self._tick(float(ticks)))

    @invariant()
    def no_seat_is_stranded(self) -> None:
        """If somebody is queued, every seat must be held (confirmed or offered)."""
        assert self.engine.reconcile() == []
        assert self.engine.held_seats() <= self.engine.capacity
        if self.engine.waitlist_size() > 0:
            assert self.engine.held_seats() == self.engine.capacity


TestOfferModeStateMachine = OfferModeStateMachine.TestCase
TestOfferModeStateMachine.settings = settings(
    max_examples=40, stateful_step_count=25, deadline=None
)
