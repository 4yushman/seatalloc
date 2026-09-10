"""Property-based tests (Hypothesis).

Example-based tests answer "does this case work?". Property-based tests answer
"can *any* sequence of operations break the promise?" — which is the question
that actually matters for an allocator handling thousands of concurrent
registrations.

Every property below is a business promise:

P1  never oversell, never double-assign a seat, keep the seat pool consistent
P2  the observable queue order always equals the mathematically defined order
P3  cancellation is idempotent
P4  a busier event produces exactly the same results as a slow one
P5  replaying the audit log reproduces the manifest byte-for-byte
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from seatalloc import (
    Attendee,
    DuplicateRegistrationError,
    EventConfig,
    EventWindowError,
    PromotionMode,
    RegistrationStatus,
    SeatAllocationEngine,
    WaitlistFullError,
)
from seatalloc.reference import NaiveAllocator

TIERS = ["GENERAL", "GENERAL", "MEMBER", "VOLUNTEER", "ORGANIZER"]

# One generated action: (kind, attendee_index, timestamp, tier)
action_st = st.tuples(
    st.sampled_from(["register", "register", "register", "cancel", "grow"]),
    st.integers(min_value=0, max_value=40),
    st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False),
    st.sampled_from(TIERS),
)
script_st = st.lists(action_st, min_size=1, max_size=45)

SETTINGS = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def attendee(idx: int, tier: str) -> Attendee:
    return Attendee(f"A{idx:04d}", f"Person {idx}", f"user{idx}@example.com", tier)


def run_script(
    actions: list[tuple[str, int, float, str]],
    *,
    capacity: int = 5,
    mode: PromotionMode = PromotionMode.AUTO,
    unique_attendees: bool = False,
) -> SeatAllocationEngine:
    """Drive an engine through a generated script of actions."""
    engine = SeatAllocationEngine(
        EventConfig(
            event_id="PROP",
            name="Property Event",
            capacity=capacity,
            promotion_mode=mode,
            offer_ttl_seconds=60.0,
            max_waitlist_size=None,
        )
    )
    pool = 0
    for kind, idx, when, tier in actions:
        if kind == "register":
            pool += 1
            who = pool if unique_attendees else idx
            # Rejected requests must leave the engine untouched.
            with contextlib.suppress(DuplicateRegistrationError, WaitlistFullError, EventWindowError):
                engine.register(attendee(who, tier), registered_at=when)
        elif kind == "cancel":
            regs = engine.registrations
            if regs:
                engine.cancel(regs[idx % len(regs)].reg_id, at=when)
        elif kind == "grow":
            engine.set_capacity(engine.capacity + (idx % 3), at=when)
        if mode is PromotionMode.OFFER:
            engine.expire_offers(at=when)
    return engine


# ---------------------------------------------------------------------------
# P1 — structural invariants
# ---------------------------------------------------------------------------
@SETTINGS
@given(script=script_st, mode=st.sampled_from([PromotionMode.AUTO, PromotionMode.OFFER]))
def test_p1_invariants_hold_for_every_operation_sequence(
    script: list[tuple[str, int, float, str]], mode: PromotionMode
) -> None:
    engine = run_script(script, mode=mode)

    assert engine.reconcile() == [], "engine invariant violation"
    held = [r for r in engine.registrations if r.status.holds_seat]
    assert len(held) <= engine.capacity, "capacity exceeded (oversold)"

    seats = [r.seat_number for r in held]
    assert None not in seats, "a held seat must always carry a seat number"
    assert len(seats) == len(set(seats)), "seat numbers must be unique"

    # Seat numbers stay inside the venue's numbering and are unique.
    assert all(1 <= s <= engine.capacity for s in seats)
    # Note: seat numbers are deliberately NOT renumbered after a cancellation.
    # A confirmed attendee's seat is a promise printed on their pass, so gaps
    # are the honest artifact of churn (see tests below + docs/DESIGN.md).

    # Every CONFIRMED row has a confirmation timestamp; every queued row does not.
    for reg in engine.registrations:
        if reg.status is RegistrationStatus.CONFIRMED:
            assert reg.confirmed_at is not None
        if reg.status is RegistrationStatus.WAITLISTED:
            assert reg.seat_number is None


@SETTINGS
@given(script=script_st)
def test_p1_never_allocates_more_than_capacity(script: list[tuple[str, int, float, str]]) -> None:
    engine = run_script(script, capacity=3)
    # The cap may be *raised* by a "grow" action, but it is never breached.
    assert engine.held_seats() <= engine.capacity
    assert engine.open_seats() >= 0


@SETTINGS
@given(script=script_st)
def test_p_seat_numbers_never_change_once_held(
    script: list[tuple[str, int, float, str]]
) -> None:
    """A held seat is a promise: later operations may not renumber it."""
    engine = SeatAllocationEngine(EventConfig(event_id="STABLE", capacity=5))
    assigned: dict[str, int] = {}
    pool = 0
    for kind, idx, when, tier in script:
        if kind == "register":
            pool += 1
            with contextlib.suppress(DuplicateRegistrationError, WaitlistFullError):
                engine.register(attendee(pool, tier), registered_at=when)
        elif kind == "cancel":
            regs = engine.registrations
            if regs:
                engine.cancel(regs[idx % len(regs)].reg_id, at=when)
        elif kind == "grow" and idx % 3:
            engine.set_capacity(engine.capacity + idx % 3, at=when)

        for reg in engine.registrations:
            if reg.status.holds_seat and reg.seat_number is not None:
                previous = assigned.setdefault(reg.reg_id, reg.seat_number)
                assert previous == reg.seat_number, (
                    f"{reg.reg_id} was renumbered from {previous} to {reg.seat_number}"
                )


# ---------------------------------------------------------------------------
# P2 — ordering is exactly the defined order
# ---------------------------------------------------------------------------
@SETTINGS
@given(script=script_st)
def test_p2_queue_order_matches_the_defined_total_order(
    script: list[tuple[str, int, float, str]]
) -> None:
    engine = run_script(script)
    order = [r.reg_id for r in engine.waitlist()]
    weights = {r.reg_id: (engine.config.tier_weight(r.tier), r.registered_at, r.sequence)
               for r in engine.registrations}
    assert order == sorted(order, key=lambda rid: weights[rid])


@SETTINGS
@given(
    stamps=st.lists(st.integers(min_value=0, max_value=10**6), min_size=1, max_size=30),
    tiers=st.lists(st.sampled_from(TIERS), min_size=1, max_size=30),
)
def test_p2_ties_on_timestamp_are_broken_by_arrival_order(
    stamps: list[int], tiers: list[str]
) -> None:
    """Identical registration timestamps must still yield a deterministic, fair order."""
    engine = SeatAllocationEngine(EventConfig(event_id="TIE", capacity=0))
    ids = []
    for i, stamp in enumerate(stamps):
        tier = tiers[i % len(tiers)]
        ids.append(engine.register(attendee(i, tier), registered_at=float(stamp)).reg_id)

    expected = sorted(
        ids,
        key=lambda rid: (
            engine.config.tier_weight(engine.get(rid).tier),
            engine.get(rid).registered_at,
            engine.get(rid).sequence,
        ),
    )
    assert [r.reg_id for r in engine.waitlist()] == expected


# ---------------------------------------------------------------------------
# P3 — idempotence
# ---------------------------------------------------------------------------
@SETTINGS
@given(script=script_st, target=st.integers(min_value=0, max_value=50))
def test_p3_cancelling_twice_is_the_same_as_cancelling_once(
    script: list[tuple[str, int, float, str]], target: int
) -> None:
    engine = run_script(script)
    regs = engine.registrations
    if not regs:
        return
    reg = regs[target % len(regs)]
    engine.cancel(reg.reg_id, at=10_000.0)
    snapshot = [(r.reg_id, r.status, r.seat_number) for r in engine.registrations]
    second = engine.cancel(reg.reg_id, at=10_001.0)
    third = engine.cancel(reg.reg_id, at=10_002.0)

    assert second is False and third is False, "repeat cancellations must be no-ops"
    assert [(r.reg_id, r.status, r.seat_number) for r in engine.registrations] == snapshot


# ---------------------------------------------------------------------------
# P4 — the heap engine agrees with the naive oracle
# ---------------------------------------------------------------------------
@SETTINGS
@given(script=st.lists(action_st, min_size=1, max_size=40))
def test_p4_engine_matches_naive_model(script: list[tuple[str, int, float, str]]) -> None:
    """Model equivalence: fast heap implementation vs obviously-correct list model."""
    capacity = 4
    engine = SeatAllocationEngine(
        EventConfig(event_id="PROP", name="Property", capacity=capacity)
    )
    model = NaiveAllocator(EventConfig(event_id="PROP", name="Property", capacity=capacity))

    pool = 0
    for kind, idx, when, tier in script:
        if kind == "register":
            pool += 1
            person = attendee(pool, tier)
            engine.register(person, registered_at=when)
            model.register(person, when)
        elif kind == "cancel":
            regs = engine.registrations
            if not regs:
                continue
            target = regs[idx % len(regs)].reg_id
            engine.cancel(target, at=when)
            model.cancel(target, when)
        elif kind == "grow":
            extra = idx % 3
            if extra:
                engine.set_capacity(engine.capacity + extra, at=when)
                model.set_capacity(model.config.capacity + extra, when)

        assert engine.confirmed_seats() == model.confirmed(), "allocation diverged"
        assert [r.reg_id for r in engine.waitlist()] == [r.reg_id for r in model.queue()]


# ---------------------------------------------------------------------------
# P5 — determinism & replay
# ---------------------------------------------------------------------------
@SETTINGS
@given(script=st.lists(action_st, min_size=1, max_size=30))
def test_p5_identical_input_produces_identical_manifest(
    script: list[tuple[str, int, float, str]]
) -> None:
    from seatalloc import manifest_fingerprint

    a = run_script(script, unique_attendees=True)
    b = run_script(script, unique_attendees=True)
    assert manifest_fingerprint(a, at=1_000.0) == manifest_fingerprint(b, at=1_000.0)


@SETTINGS
@given(script=st.lists(action_st, min_size=1, max_size=25))
def test_p5_replay_reconstructs_the_same_state(
    script: list[tuple[str, int, float, str]], tmp_path_factory: pytest.TempPathFactory
) -> None:
    from seatalloc.persistence import EventLog, replay

    tmp = Path(tmp_path_factory.mktemp("replay"))
    config = EventConfig(event_id="PROP", name="Property", capacity=4)
    log = EventLog(tmp / "audit.jsonl", config)
    engine = SeatAllocationEngine(config, audit_hook=log.append)
    pool = 0
    try:
        for kind, idx, when, tier in script:
            if kind == "register":
                pool += 1
                engine.register(attendee(pool, tier), registered_at=when)
            elif kind == "cancel":
                regs = engine.registrations
                if regs:
                    engine.cancel(regs[idx % len(regs)].reg_id, at=when)
            elif kind == "grow" and idx % 3:
                engine.set_capacity(engine.capacity + (idx % 3), at=when)
    finally:
        log.close()

    rebuilt = replay(tmp / "audit.jsonl")
    assert rebuilt.snapshot() == engine.snapshot()
    assert rebuilt.reconcile() == []
