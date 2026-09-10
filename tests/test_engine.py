"""Behavioural tests for the allocation engine.

Each test names the rule it protects, so a failing test tells you which business
promise broke rather than just which line moved.
"""

from __future__ import annotations

import pytest

from seatalloc import (
    CapacityConflictError,
    DuplicateRegistrationError,
    EventConfig,
    EventWindowError,
    PromotionMode,
    RegistrationStatus,
    SeatAllocationEngine,
    StatusConflictError,
    UnknownRegistrationError,
    WaitlistFullError,
)
from seatalloc.models import to_iso

from .conftest import BASE, make_attendee, make_engine


# ---------------------------------------------------------------------------
# Seat caps
# ---------------------------------------------------------------------------
def test_confirms_until_capacity_then_waitlists(engine: SeatAllocationEngine) -> None:
    regs = [engine.register(make_attendee(i), registered_at=BASE + i) for i in range(5)]

    assert [r.status for r in regs[:3]] == [RegistrationStatus.CONFIRMED] * 3
    assert [r.status for r in regs[3:]] == [RegistrationStatus.WAITLISTED] * 2
    assert [r.seat_number for r in regs[:3]] == [1, 2, 3]
    assert engine.open_seats() == 0
    assert engine.held_seats() == 3


def test_capacity_zero_waitlists_everybody() -> None:
    engine = make_engine(capacity=0)
    reg = engine.register(make_attendee(1), registered_at=BASE)
    assert reg.status is RegistrationStatus.WAITLISTED
    assert engine.waitlist_size() == 1


def test_recycled_seat_number_is_reused_lowest_first(engine: SeatAllocationEngine) -> None:
    first = engine.register(make_attendee(1), registered_at=BASE)
    assert first.seat_number == 1
    engine.register(make_attendee(2), registered_at=BASE + 1)
    engine.cancel(first.reg_id, at=BASE + 2)
    # A cancelled registration surrenders its seat number...
    assert first.seat_number is None
    third = engine.register(make_attendee(3), registered_at=BASE + 3)
    # ...and the recycled number is handed to the next attendee, lowest first.
    assert third.seat_number == 1


# ---------------------------------------------------------------------------
# Ordering / fairness
# ---------------------------------------------------------------------------
def test_waitlist_is_fifo_within_a_tier() -> None:
    engine = make_engine(capacity=1)
    engine.register(make_attendee(1), registered_at=BASE)
    late = engine.register(make_attendee(2), registered_at=BASE + 10)
    middle = engine.register(make_attendee(3), registered_at=BASE + 5)
    assert [r.reg_id for r in engine.waitlist()] == [middle.reg_id, late.reg_id]


def test_higher_tier_jumps_the_queue() -> None:
    engine = make_engine(capacity=1)
    engine.register(make_attendee(1), registered_at=BASE)
    engine.register(make_attendee(2), registered_at=BASE + 1)                 # GENERAL
    engine.register(make_attendee(3, "MEMBER"), registered_at=BASE + 2)       # MEMBER
    assert engine.waitlist()[0].attendee.attendee_id == "A0003"


def test_equal_timestamps_fall_back_to_arrival_sequence() -> None:
    """The ordering must be *total*: identical timestamps can never tie-break randomly."""
    engine = make_engine(capacity=1)
    engine.register(make_attendee(1), registered_at=BASE)
    a = engine.register(make_attendee(2), registered_at=BASE + 1)
    b = engine.register(make_attendee(3), registered_at=BASE + 1)
    c = engine.register(make_attendee(4), registered_at=BASE + 1)
    assert [r.reg_id for r in engine.waitlist()] == [a.reg_id, b.reg_id, c.reg_id]


def test_out_of_order_arrivals_are_ordered_by_timestamp_not_call_order() -> None:
    engine = make_engine(capacity=1)
    engine.register(make_attendee(1), registered_at=BASE)
    later = engine.register(make_attendee(2), registered_at=BASE + 100)
    earlier = engine.register(make_attendee(3), registered_at=BASE + 10)
    assert engine.waitlist()[0].reg_id == earlier.reg_id
    assert engine.waitlist()[1].reg_id == later.reg_id


# ---------------------------------------------------------------------------
# Cancellation + promotion
# ---------------------------------------------------------------------------
def test_cancellation_promotes_the_next_waitlisted_attendee() -> None:
    engine = make_engine(capacity=1)
    holder = engine.register(make_attendee(1), registered_at=BASE)
    nxt = engine.register(make_attendee(2), registered_at=BASE + 1)
    engine.cancel(holder.reg_id, at=BASE + 2)

    assert holder.status is RegistrationStatus.CANCELLED
    assert nxt.status is RegistrationStatus.CONFIRMED
    assert nxt.promoted_from_waitlist is True
    assert nxt.seat_number == 1
    assert engine.open_seats() == 0
    assert engine.stats()["total_promotions"] == 1


def test_cancel_is_idempotent_and_never_double_promotes() -> None:
    engine = make_engine(capacity=1)
    holder = engine.register(make_attendee(1), registered_at=BASE)
    waiter = engine.register(make_attendee(2), registered_at=BASE + 1)
    assert engine.cancel(holder.reg_id, at=BASE + 2) is True
    assert engine.cancel(holder.reg_id, at=BASE + 3) is False   # second click: no-op
    assert engine.cancel(holder.reg_id, at=BASE + 4) is False
    assert waiter.status is RegistrationStatus.CONFIRMED
    assert engine.stats()["total_promotions"] == 1


def test_cancelling_a_waitlisted_attendee_frees_nothing_and_skips_them() -> None:
    engine = make_engine(capacity=1)
    engine.register(make_attendee(1), registered_at=BASE)
    second = engine.register(make_attendee(2), registered_at=BASE + 1)
    third = engine.register(make_attendee(3), registered_at=BASE + 2)

    assert engine.cancel(second.reg_id, at=BASE + 3) is True
    assert third.status is RegistrationStatus.WAITLISTED
    assert [r.reg_id for r in engine.waitlist()] == [third.reg_id]
    assert engine.waitlist_position(third.reg_id) == 1


def test_promotion_skips_attendees_who_left_the_queue() -> None:
    engine = make_engine(capacity=1)
    holder = engine.register(make_attendee(1), registered_at=BASE)
    quitter = engine.register(make_attendee(2), registered_at=BASE + 1)
    survivor = engine.register(make_attendee(3), registered_at=BASE + 2)
    engine.cancel(quitter.reg_id, at=BASE + 3)     # tombstone left inside the heap
    engine.cancel(holder.reg_id, at=BASE + 4)      # promotes the *live* next in line
    assert survivor.status is RegistrationStatus.CONFIRMED
    assert quitter.status is RegistrationStatus.CANCELLED


def test_reinstate_restores_queue_place_without_breaking_order() -> None:
    engine = make_engine(capacity=1)
    engine.register(make_attendee(1), registered_at=BASE)
    early = engine.register(make_attendee(2), registered_at=BASE + 5)
    engine.register(make_attendee(3), registered_at=BASE + 9)
    engine.cancel(early.reg_id, at=BASE + 10)
    engine.reinstate(early.reg_id, at=BASE + 11)
    assert engine.waitlist()[0].reg_id == early.reg_id   # keeps its original timestamp


# ---------------------------------------------------------------------------
# Offer mode (time-boxed promotions)
# ---------------------------------------------------------------------------
def test_offer_mode_holds_a_seat_until_accepted() -> None:
    engine = make_engine(capacity=1, promotion_mode=PromotionMode.OFFER, offer_ttl_seconds=300)
    holder = engine.register(make_attendee(1), registered_at=BASE)
    waiter = engine.register(make_attendee(2), registered_at=BASE + 1)
    engine.cancel(holder.reg_id, at=BASE + 2)

    assert waiter.status is RegistrationStatus.OFFERED
    assert waiter.offer_expires_at == BASE + 302
    assert engine.open_seats() == 0            # the seat is *held*, not free
    assert engine.waitlist_size() == 0

    engine.accept_offer(waiter.reg_id, at=BASE + 10)
    assert waiter.status is RegistrationStatus.CONFIRMED
    assert waiter.seat_number == 1


def test_expired_offer_is_recycled_to_the_next_attendee() -> None:
    engine = make_engine(capacity=1, promotion_mode=PromotionMode.OFFER, offer_ttl_seconds=60)
    holder = engine.register(make_attendee(1), registered_at=BASE)
    slow = engine.register(make_attendee(2), registered_at=BASE + 1)
    quick = engine.register(make_attendee(3), registered_at=BASE + 2)
    engine.cancel(holder.reg_id, at=BASE + 3)

    assert slow.status is RegistrationStatus.OFFERED
    expired = engine.expire_offers(at=BASE + 3 + 61)
    assert expired == [slow.reg_id]
    assert slow.status is RegistrationStatus.EXPIRED
    assert quick.status is RegistrationStatus.OFFERED   # seat did not sit idle
    assert engine.stats()["expired_offers"] == 1


def test_expire_offers_before_the_deadline_is_a_no_op() -> None:
    engine = make_engine(capacity=1, promotion_mode=PromotionMode.OFFER, offer_ttl_seconds=600)
    holder = engine.register(make_attendee(1), registered_at=BASE)
    waiter = engine.register(make_attendee(2), registered_at=BASE + 1)
    engine.cancel(holder.reg_id, at=BASE + 2)
    assert engine.expire_offers(at=BASE + 3) == []
    assert waiter.status is RegistrationStatus.OFFERED


def test_declining_an_offer_passes_the_seat_on() -> None:
    engine = make_engine(capacity=1, promotion_mode=PromotionMode.OFFER)
    holder = engine.register(make_attendee(1), registered_at=BASE)
    first = engine.register(make_attendee(2), registered_at=BASE + 1)
    second = engine.register(make_attendee(3), registered_at=BASE + 2)
    engine.cancel(holder.reg_id, at=BASE + 3)
    engine.decline_offer(first.reg_id, at=BASE + 4)

    assert first.status is RegistrationStatus.CANCELLED
    assert second.status is RegistrationStatus.OFFERED
    assert engine.held_seats() == 1


def test_accept_offer_rejects_invalid_transitions() -> None:
    engine = make_engine(capacity=1, promotion_mode=PromotionMode.OFFER)
    reg = engine.register(make_attendee(1), registered_at=BASE)
    with pytest.raises(StatusConflictError):
        engine.accept_offer(reg.reg_id, at=BASE + 1)   # already CONFIRMED


# ---------------------------------------------------------------------------
# Capacity changes
# ---------------------------------------------------------------------------
def test_increasing_capacity_pulls_people_off_the_waitlist() -> None:
    engine = make_engine(capacity=1)
    engine.register(make_attendee(1), registered_at=BASE)
    a = engine.register(make_attendee(2), registered_at=BASE + 1)
    b = engine.register(make_attendee(3), registered_at=BASE + 2)

    promoted = engine.set_capacity(3, at=BASE + 3)
    assert promoted == 2
    assert (a.status, b.status) == (RegistrationStatus.CONFIRMED, RegistrationStatus.CONFIRMED)
    assert engine.waitlist_size() == 0
    assert engine.open_seats() == 0


def test_shrinking_below_held_seats_is_refused() -> None:
    engine = make_engine(capacity=3)
    for i in range(3):
        engine.register(make_attendee(i), registered_at=BASE + i)
    with pytest.raises(CapacityConflictError):
        engine.set_capacity(2, at=BASE + 5)
    assert engine.capacity == 3                 # nothing changed on refusal
    assert engine.held_seats() == 3


def test_shrinking_within_limits_works_and_keeps_confirmations() -> None:
    engine = make_engine(capacity=4)
    for i in range(2):
        engine.register(make_attendee(i), registered_at=BASE + i)
    engine.set_capacity(2, at=BASE + 5)
    assert engine.capacity == 2 and engine.open_seats() == 0
    assert all(r.seat_number in (1, 2) for r in engine.confirmed())


def test_setting_the_same_capacity_is_a_no_op() -> None:
    engine = make_engine(capacity=2)
    assert engine.set_capacity(2, at=BASE) == 0


# ---------------------------------------------------------------------------
# Guards: duplicates, windows, bounded waitlists
# ---------------------------------------------------------------------------
def test_duplicate_registration_is_rejected() -> None:
    engine = make_engine(capacity=1)
    engine.register(make_attendee(1), registered_at=BASE)
    with pytest.raises(DuplicateRegistrationError):
        engine.register(make_attendee(1), registered_at=BASE + 1)


def test_attendee_may_register_again_after_cancelling() -> None:
    engine = make_engine(capacity=1)
    first = engine.register(make_attendee(1), registered_at=BASE)
    engine.cancel(first.reg_id, at=BASE + 1)
    second = engine.register(make_attendee(1), registered_at=BASE + 2)
    assert second.status is RegistrationStatus.CONFIRMED
    assert second.reg_id != first.reg_id


def test_registration_window_is_enforced() -> None:
    engine = SeatAllocationEngine(
        EventConfig(
            event_id="WINDOW",
            capacity=5,
            registration_opens_at=BASE,
            registration_closes_at=BASE + 100,
        )
    )
    engine.register(make_attendee(1), registered_at=BASE + 50)
    with pytest.raises(EventWindowError):
        engine.register(make_attendee(2), registered_at=BASE + 101)
    with pytest.raises(EventWindowError):
        engine.register(make_attendee(3), registered_at=BASE - 1)


def test_bounded_waitlist_applies_back_pressure() -> None:
    engine = make_engine(capacity=1, max_waitlist_size=2)
    engine.register(make_attendee(1), registered_at=BASE)
    engine.register(make_attendee(2), registered_at=BASE + 1)
    engine.register(make_attendee(3), registered_at=BASE + 2)
    with pytest.raises(WaitlistFullError):
        engine.register(make_attendee(4), registered_at=BASE + 3)
    rejected = engine.get("TEST-R000004")
    assert rejected.status is RegistrationStatus.REJECTED
    assert rejected.note == "waitlist_full"


def test_try_register_reports_reasons_instead_of_raising() -> None:
    engine = make_engine(capacity=1, max_waitlist_size=1)
    assert engine.try_register(make_attendee(1), registered_at=BASE)[1] is None
    assert engine.try_register(make_attendee(1), registered_at=BASE + 1)[1] == "duplicate"
    assert engine.try_register(make_attendee(2), registered_at=BASE + 1)[1] is None
    assert engine.try_register(make_attendee(3), registered_at=BASE + 2)[1] == "waitlist_full"


def test_unknown_registration_id_raises() -> None:
    engine = make_engine()
    with pytest.raises(UnknownRegistrationError):
        engine.cancel("TEST-R999999")


# ---------------------------------------------------------------------------
# Post-event states and stats
# ---------------------------------------------------------------------------
def test_check_in_and_no_show_are_recorded_without_moving_seats() -> None:
    engine = make_engine(capacity=2)
    a = engine.register(make_attendee(1), registered_at=BASE)
    b = engine.register(make_attendee(2), registered_at=BASE + 1)
    engine.check_in(a.reg_id, at=BASE + 100)
    engine.mark_no_show(b.reg_id, at=BASE + 100)

    assert a.status is RegistrationStatus.ATTENDED
    assert b.status is RegistrationStatus.NO_SHOW
    stats = engine.stats(at=BASE + 200)
    assert stats["attended"] == 1 and stats["no_show"] == 1
    assert engine.confirmed() == []


def test_stats_summarise_the_event() -> None:
    engine = make_engine(capacity=2)
    for i in range(4):
        engine.register(make_attendee(i), registered_at=BASE + i)
    first = engine.confirmed()[0]
    engine.cancel(first.reg_id, at=BASE + 50)

    stats = engine.stats(at=BASE + 100)
    assert stats["capacity"] == 2
    assert stats["confirmed"] == 2
    assert stats["waitlisted"] == 1
    assert stats["cancelled"] == 1
    assert stats["total_registrations"] == 4
    assert stats["fill_rate"] == 1.0
    assert stats["total_promotions"] == 1
    assert stats["max_wait_to_confirm_seconds"] > 0


def test_manifest_helpers_are_seat_ordered() -> None:
    engine = make_engine(capacity=3)
    for i in range(3):
        engine.register(make_attendee(i), registered_at=BASE + i)
    assert [r.seat_number for r in engine.confirmed()] == [1, 2, 3]
    assert [r.attendee.attendee_id for r in engine.confirmed()] == ["A0000", "A0001", "A0002"]


def test_waitlist_position_only_applies_to_queued_people() -> None:
    engine = make_engine(capacity=1)
    confirmed = engine.register(make_attendee(1), registered_at=BASE)
    waiting = engine.register(make_attendee(2), registered_at=BASE + 1)
    assert engine.waitlist_position(waiting.reg_id) == 1
    assert engine.waitlist_position(confirmed.reg_id) is None
    assert engine.waitlist_position("TEST-R999999") is None


def test_reconcile_is_clean_after_a_busy_session() -> None:
    engine = make_engine(capacity=5)
    for i in range(30):
        engine.register(make_attendee(i, "MEMBER" if i % 3 == 0 else "GENERAL"), registered_at=BASE + i)
    for reg in engine.confirmed()[:4]:
        engine.cancel(reg.reg_id, at=BASE + 100)
    for reg in engine.waitlist()[:3]:
        engine.cancel(reg.reg_id, at=BASE + 101)
    engine.set_capacity(8, at=BASE + 102)

    assert engine.reconcile() == []
    assert engine.held_seats() <= engine.capacity


def test_registration_ids_are_unique_and_never_reused() -> None:
    engine = make_engine(capacity=1)
    ids = set()
    for i in range(20):
        reg = engine.register(make_attendee(i), registered_at=BASE + i)
        ids.add(reg.reg_id)
    assert len(ids) == 20
    assert "TEST-R000001" in ids


def test_audit_trail_records_primary_operations() -> None:
    engine = make_engine(capacity=1)
    first = engine.register(make_attendee(1), registered_at=BASE)
    engine.register(make_attendee(2), registered_at=BASE + 1)
    engine.cancel(first.reg_id, at=BASE + 2)
    ops = [event["op"] for event in engine.audit_trail]
    # "register" is the primary op; "confirm_seat" / "waitlist_push" are the
    # derived detail events that record *how* that registration was placed.
    assert [op for op in ops if op == "register"] == ["register", "register"]
    assert ops.index("confirm_seat") < ops.index("register", 1)
    assert "waitlist_push" in ops
    assert "cancel" in ops
    cancel_event = next(e for e in engine.audit_trail if e["op"] == "cancel")
    assert cancel_event["status"] == "CANCELLED"
    assert cancel_event["seat_recycled"] is True


def test_to_iso_round_trips() -> None:
    assert to_iso(None) == ""
    assert to_iso(BASE).endswith("+00:00")
