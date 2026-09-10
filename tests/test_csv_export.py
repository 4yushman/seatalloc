"""CSV export tests.

The manifest is a *deliverable* — it gets printed, emailed and pasted into
spreadsheets — so it is tested like one: exact headers, stable ordering,
hostile characters, and byte-for-byte reproducibility.
"""

from __future__ import annotations

import csv
from pathlib import Path

from seatalloc import Attendee, EventConfig, SeatAllocationEngine
from seatalloc.csv_export import (
    AUDIT_COLUMNS,
    MANIFEST_COLUMNS,
    SUMMARY_COLUMNS,
    WAITLIST_COLUMNS,
    export_all,
    export_audit,
    export_manifest,
    export_summary,
    export_waitlist,
    iter_csv,
    manifest_fingerprint,
    manifest_rows,
    render_manifest_csv,
    summary_rows,
    waitlist_rows,
)
from seatalloc.models import to_iso

from .conftest import BASE, make_attendee, make_engine


def busy_engine(capacity: int = 3) -> SeatAllocationEngine:
    engine = make_engine(capacity=capacity)
    for i in range(6):
        engine.register(make_attendee(i, "MEMBER" if i % 2 else "GENERAL"), registered_at=BASE + i)
    engine.cancel(engine.confirmed()[0].reg_id, at=BASE + 100)
    return engine


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def test_manifest_has_the_documented_header(tmp_path: Path) -> None:
    path = export_manifest(busy_engine(), tmp_path / "manifest.csv")
    with path.open(newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert tuple(header) == MANIFEST_COLUMNS


def test_manifest_lists_exactly_the_held_seats_in_seat_order(tmp_path: Path) -> None:
    engine = busy_engine()
    path = export_manifest(engine, tmp_path / "manifest.csv", at=BASE + 200)
    rows = list(iter_csv(path))

    assert len(rows) == engine.held_seats()
    assert [int(r["seat_number"]) for r in rows] == sorted(
        r.seat_number for r in engine.registrations if r.status.holds_seat
    )
    assert {r["reg_id"] for r in rows} == {r.reg_id for r in engine.registrations if r.status.holds_seat}


def test_manifest_carries_the_operational_fields(tmp_path: Path) -> None:
    engine = busy_engine()
    engine.check_in(engine.confirmed()[0].reg_id, at=BASE + 150)
    rows = manifest_rows(engine, at=BASE + 200)
    row = rows[0]
    assert row["status"] in {"CONFIRMED", "ATTENDED"}
    assert row["registered_at_utc"].endswith("+00:00")
    assert float(row["wait_seconds"]) >= 0
    assert row["tier"] in {"MEMBER", "GENERAL"}


def test_manifest_marks_waitlist_promotions(tmp_path: Path) -> None:
    engine = busy_engine()
    promoted = [r for r in engine.registrations if r.promoted_from_waitlist]
    assert promoted, "scenario should have promoted somebody"
    rows = {r["reg_id"]: r for r in manifest_rows(engine, at=BASE + 200)}
    assert rows[promoted[0].reg_id]["promoted_from_waitlist"] is True


def test_manifest_escapes_awkward_names(tmp_path: Path) -> None:
    engine = SeatAllocationEngine(EventConfig(event_id="ESC", capacity=2))
    engine.register(
        Attendee("A1", 'Rahul "RJ" Sharma, Jr.', "rahul+devx@example.com", "MEMBER"),
        registered_at=BASE,
    )
    engine.register(Attendee("A2", "Ana María O'Néill", "ana@example.com", "GENERAL"),
                    registered_at=BASE + 1)
    path = export_manifest(engine, tmp_path / "manifest.csv", at=BASE + 2)

    rows = list(iter_csv(path))
    assert rows[0]["name"] == 'Rahul "RJ" Sharma, Jr.'
    assert rows[0]["email"] == "rahul+devx@example.com"
    assert rows[1]["name"] == "Ana María O'Néill"


def test_manifest_is_written_as_utf8(tmp_path: Path) -> None:
    engine = SeatAllocationEngine(EventConfig(event_id="UTF", capacity=1))
    engine.register(Attendee("A1", "Ayşe Çelik — İstanbul", "ayse@example.com"), registered_at=BASE)
    path = export_manifest(engine, tmp_path / "manifest.csv", at=BASE + 1)
    assert "Ayşe Çelik — İstanbul" in path.read_text(encoding="utf-8")  # utf-8, not latin-1


def test_manifest_is_byte_identical_across_runs(tmp_path: Path) -> None:
    a = export_manifest(busy_engine(), tmp_path / "a.csv", at=BASE + 200)
    b = export_manifest(busy_engine(), tmp_path / "b.csv", at=BASE + 200)
    assert a.read_bytes() == b.read_bytes()
    assert manifest_fingerprint(busy_engine(), at=BASE + 200) == manifest_fingerprint(
        busy_engine(), at=BASE + 200
    )


def test_fingerprint_changes_when_allocation_changes() -> None:
    before = manifest_fingerprint(busy_engine(), at=BASE + 200)
    engine = busy_engine()
    engine.cancel(engine.confirmed()[0].reg_id, at=BASE + 300)
    after = manifest_fingerprint(engine, at=BASE + 400)
    assert before != after


def test_render_manifest_csv_matches_the_written_file(tmp_path: Path) -> None:
    engine = busy_engine()
    path = export_manifest(engine, tmp_path / "manifest.csv", at=BASE + 200)
    # Read with newline="" so the file's CRLF terminators (Excel-friendly, the
    # csv module default) are compared verbatim instead of being translated.
    with path.open(newline="", encoding="utf-8") as fh:
        assert render_manifest_csv(engine, at=BASE + 200) == fh.read()


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------
def test_waitlist_export_is_position_ordered(tmp_path: Path) -> None:
    engine = busy_engine(capacity=2)
    path = export_waitlist(engine, tmp_path / "waitlist.csv", at=BASE + 200)
    rows = list(iter_csv(path))

    assert tuple(rows[0].keys()) == WAITLIST_COLUMNS
    assert [int(r["queue_position"]) for r in rows] == list(range(1, len(rows) + 1))
    assert [r["reg_id"] for r in rows] == [r.reg_id for r in engine.waitlist()]


def test_waitlist_rows_report_priority_weights() -> None:
    engine = make_engine(capacity=1)
    engine.register(make_attendee(0), registered_at=BASE)
    engine.register(make_attendee(1, "ORGANIZER"), registered_at=BASE + 1)
    rows = waitlist_rows(engine, at=BASE + 10)
    assert rows[0]["tier"] == "ORGANIZER"
    assert rows[0]["priority_weight"] == 0


def test_waitlist_export_is_empty_when_everyone_has_a_seat(tmp_path: Path) -> None:
    engine = make_engine(capacity=5)
    engine.register(make_attendee(0), registered_at=BASE)
    path = export_waitlist(engine, tmp_path / "waitlist.csv", at=BASE + 1)
    assert len(list(iter_csv(path))) == 0
    assert WAITLIST_COLUMNS[0] in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Audit + summary + export_all
# ---------------------------------------------------------------------------
def test_audit_export_includes_terminal_states(tmp_path: Path) -> None:
    engine = busy_engine()
    target = engine.confirmed()[0].reg_id
    engine.cancel(target, at=BASE + 300)
    path = export_audit(engine, tmp_path / "audit.csv")
    rows = {r["reg_id"]: r for r in iter_csv(path)}

    assert tuple(rows[target].keys()) == AUDIT_COLUMNS
    assert rows[target]["status"] == "CANCELLED"
    assert rows[target]["seat_number"] == ""
    assert len(rows) == len(engine.registrations)


def test_summary_export_has_one_row(tmp_path: Path) -> None:
    engine = busy_engine()
    path = export_summary(engine, tmp_path / "summary.csv", at=BASE + 200)
    rows = list(iter_csv(path))
    assert len(rows) == 1
    assert tuple(rows[0].keys()) == SUMMARY_COLUMNS
    assert int(rows[0]["capacity"]) == engine.capacity
    assert rows[0]["exported_at_utc"] == to_iso(BASE + 200)


def test_summary_rows_reflect_live_stats() -> None:
    engine = busy_engine()
    row = summary_rows(engine, at=BASE + 200)[0]
    assert int(row["confirmed"]) == len(engine.confirmed())
    assert int(row["waitlisted"]) == engine.waitlist_size()


def test_export_all_writes_the_four_artifacts(tmp_path: Path) -> None:
    engine = busy_engine()
    paths = export_all(engine, tmp_path, at=BASE + 200, stem="devx")
    assert set(paths) == {"manifest", "waitlist", "audit", "summary"}
    for path in paths.values():
        assert path.exists() and path.stat().st_size > 0
    assert paths["manifest"].name == "devx_manifest.csv"


def test_export_creates_missing_directories(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "dir" / "manifest.csv"
    export_manifest(make_engine(), target, at=BASE)
    assert target.exists()


def test_exported_manifest_can_be_re_imported_as_registrations(tmp_path: Path) -> None:
    """Round-trip: the manifest is detailed enough to rebuild a registration set."""
    engine = busy_engine()
    path = export_manifest(engine, tmp_path / "manifest.csv", at=BASE + 200)
    rebuilt = SeatAllocationEngine(EventConfig(event_id="RT", capacity=engine.capacity))
    for row in iter_csv(path):
        rebuilt.register(
            Attendee(row["attendee_id"], row["name"], row["email"], row["tier"]),
            registered_at=BASE + int(row["seat_number"]),
        )
    assert rebuilt.held_seats() == engine.held_seats()
    assert {r.attendee.attendee_id for r in rebuilt.confirmed()} == {
        r.attendee.attendee_id for r in engine.confirmed()
    }
