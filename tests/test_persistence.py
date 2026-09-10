"""Audit-log and replay tests.

The claim under test: *the JSONL audit log is a complete, replayable record of
the event.* If that holds, an organiser can answer "why did this person get a
seat?" months later, and a disputed manifest can be regenerated and hash-checked
instead of argued about.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seatalloc import (
    Attendee,
    EventConfig,
    PromotionMode,
    SeatAllocationEngine,
    manifest_fingerprint,
)
from seatalloc.persistence import EventLog, read_events, replay, write_events

from .conftest import BASE, make_attendee


def build_event(path: Path, *, mode: PromotionMode = PromotionMode.AUTO) -> SeatAllocationEngine:
    """A scripted event that exercises every primary operation we log."""
    config = EventConfig(
        event_id="LOG",
        name="Logged Event",
        capacity=3,
        promotion_mode=mode,
        offer_ttl_seconds=100.0,
    )
    with EventLog(path, config) as log:
        engine = SeatAllocationEngine(config, audit_hook=log.append)
        for i in range(8):
            engine.register(make_attendee(i, "MEMBER" if i % 3 == 0 else "GENERAL"),
                            registered_at=BASE + i)
        engine.cancel("LOG-R000002", at=BASE + 50)
        engine.cancel("LOG-R000005", at=BASE + 51)          # a waitlisted cancellation
        engine.set_capacity(5, at=BASE + 60)
        if mode is PromotionMode.OFFER:
            engine.expire_offers(at=BASE + 60 + 101)
            for offer in list(engine.offers()):
                engine.accept_offer(offer.reg_id, at=BASE + 200)
        engine.check_in(engine.confirmed()[0].reg_id, at=BASE + 300)
        engine.mark_no_show(engine.confirmed()[-1].reg_id, at=BASE + 300)
    return engine


# ---------------------------------------------------------------------------
# Log format
# ---------------------------------------------------------------------------
def test_log_starts_with_a_self_describing_header(tmp_path: Path) -> None:
    build_event(tmp_path / "audit.jsonl")
    events = read_events(tmp_path / "audit.jsonl")

    assert events[0]["op"] == "event_created"
    assert events[0]["config"]["event_id"] == "LOG"
    assert events[0]["config"]["capacity"] == 3


def test_every_log_line_is_valid_json(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    build_event(log_path)
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == len(read_events(log_path))
    for line in lines:
        json.loads(line)


def test_log_records_the_primary_operations(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    build_event(log_path)
    ops = {event["op"] for event in read_events(log_path)}
    assert {"event_created", "register", "cancel", "set_capacity", "check_in", "mark_no_show"} <= ops


def test_register_events_carry_the_attendee_payload(tmp_path: Path) -> None:
    """Replay is only possible because the log stores *what* was registered."""
    log_path = tmp_path / "audit.jsonl"
    build_event(log_path)
    first_register = next(e for e in read_events(log_path) if e["op"] == "register")
    for field in ("attendee_id", "name", "email", "tier", "registered_at"):
        assert field in first_register


def test_blank_lines_are_ignored(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    build_event(log_path)
    text = log_path.read_text(encoding="utf-8")
    log_path.write_text(text + "\n\n", encoding="utf-8")
    assert len(read_events(log_path)) == len(read_events(log_path))


def test_truncated_or_missing_header_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    write_events(bad, [{"op": "register", "at": 1.0}])
    with pytest.raises(ValueError, match="event_created"):
        replay(bad)

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        replay(empty)


def test_corrupt_json_reports_the_line_number(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.jsonl"
    bad.write_text('{"op": "event_created", "config": {}}\n{"op": broken}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=":2"):
        read_events(bad)


# ---------------------------------------------------------------------------
# Replay equivalence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", [PromotionMode.AUTO, PromotionMode.OFFER])
def test_replay_reconstructs_the_identical_state(tmp_path: Path, mode: PromotionMode) -> None:
    log_path = tmp_path / f"audit_{mode.value}.jsonl"
    original = build_event(log_path, mode=mode)
    rebuilt = replay(log_path)

    assert rebuilt.snapshot() == original.snapshot()
    assert rebuilt.stats() == original.stats()
    assert rebuilt.reconcile() == []


def test_replayed_manifest_is_byte_identical(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    original = build_event(log_path)
    rebuilt = replay(log_path)
    assert manifest_fingerprint(rebuilt, at=BASE + 400) == manifest_fingerprint(
        original, at=BASE + 400
    )


def test_replay_is_idempotent(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    build_event(log_path)
    first = replay(log_path)
    second = replay(log_path)
    assert first.snapshot() == second.snapshot()


def test_unknown_derived_events_are_ignored_by_replay(tmp_path: Path) -> None:
    """Forward compatibility: a newer writer may add detail events."""
    log_path = tmp_path / "audit.jsonl"
    original = build_event(log_path)
    events = read_events(log_path)
    events.insert(3, {"op": "some_future_metric", "at": BASE, "value": 42})
    write_events(log_path, events)

    rebuilt = replay(log_path)
    assert rebuilt.snapshot() == original.snapshot()


def test_append_mode_continues_an_existing_log(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    config = EventConfig(event_id="APPEND", capacity=1)
    with EventLog(log_path, config) as log:
        engine = SeatAllocationEngine(config, audit_hook=log.append)
        engine.register(make_attendee(1), registered_at=BASE)

    with EventLog(log_path, config, truncate=False) as log:
        engine2 = SeatAllocationEngine(config, audit_hook=log.append)
        engine2.register(make_attendee(2), registered_at=BASE + 1)

    events = read_events(log_path)
    assert sum(1 for e in events if e["op"] == "event_created") == 1
    assert sum(1 for e in events if e["op"] == "register") == 2


def test_event_log_is_usable_as_a_context_manager(tmp_path: Path) -> None:
    config = EventConfig(event_id="CTX", capacity=1)
    with EventLog(tmp_path / "a.jsonl", config) as log:
        log.append({"op": "register", "at": 1.0})
        log.flush()
        assert (tmp_path / "a.jsonl").read_text(encoding="utf-8").strip()
    assert log._closed is True


def test_attendee_payload_survives_a_round_trip(tmp_path: Path) -> None:
    """Unicode names, plus-addressed emails and odd tiers must survive replay."""
    log_path = tmp_path / "audit.jsonl"
    config = EventConfig(event_id="RT", capacity=2)
    with EventLog(log_path, config) as log:
        engine = SeatAllocationEngine(config, audit_hook=log.append)
        engine.register(
            Attendee("A1", "Ayşe Çelik — İstanbul", "ayse+devx@example.com", "MEMBER"),
            registered_at=BASE,
        )
        engine.register(Attendee("A2", "Rahul \"RJ\" Sharma", "rj@example.com"), registered_at=BASE + 1)

    rebuilt = replay(log_path)
    names = [r.attendee.name for r in rebuilt.registrations]
    assert names == ["Ayşe Çelik — İstanbul", 'Rahul "RJ" Sharma']
    assert rebuilt.get("RT-R000001").tier == "MEMBER"
