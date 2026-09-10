"""CSV exports: attendee manifest, waitlist, full audit and event summary.

All writers stream rows through :mod:`csv` (never ``pandas``) so the engine has
**zero third-party runtime dependencies** and can run inside a Lambda, a cron
job or a WhatsApp-bot backend with nothing but the standard library.

Complexity: every export is ``O(n log n)`` — dominated by the sort that turns
heap order into a stable, human-readable order — and ``O(1)`` extra memory per
row (rows are yielded lazily to the writer).

Determinism: exports contain no wall-clock noise unless you pass
``generated_at``, and :func:`manifest_fingerprint` returns a SHA-256 over the
manifest bytes, which is what makes "same input -> byte-identical manifest" a
*testable* claim rather than a hopeful one.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from .engine import SeatAllocationEngine
from .models import Registration, RegistrationStatus, to_iso

__all__ = [
    "MANIFEST_COLUMNS",
    "WAITLIST_COLUMNS",
    "AUDIT_COLUMNS",
    "SUMMARY_COLUMNS",
    "manifest_rows",
    "waitlist_rows",
    "audit_rows",
    "summary_rows",
    "export_manifest",
    "export_waitlist",
    "export_audit",
    "export_summary",
    "export_all",
    "manifest_fingerprint",
    "render_manifest_csv",
]

MANIFEST_COLUMNS: tuple[str, ...] = (
    "seat_number",
    "reg_id",
    "attendee_id",
    "name",
    "email",
    "tier",
    "status",
    "registered_at_utc",
    "confirmed_at_utc",
    "wait_seconds",
    "promoted_from_waitlist",
    "queue_size_at_registration",
    "note",
)

WAITLIST_COLUMNS: tuple[str, ...] = (
    "queue_position",
    "reg_id",
    "attendee_id",
    "name",
    "email",
    "tier",
    "priority_weight",
    "registered_at_utc",
    "waiting_seconds",
    "seat_if_promoted",
)

AUDIT_COLUMNS: tuple[str, ...] = (
    "reg_id",
    "attendee_id",
    "name",
    "email",
    "tier",
    "status",
    "registered_at_utc",
    "confirmed_at_utc",
    "decided_at_utc",
    "seat_number",
    "note",
)

SUMMARY_COLUMNS: tuple[str, ...] = (
    "event_id",
    "capacity",
    "confirmed",
    "offers_outstanding",
    "waitlisted",
    "cancelled",
    "expired",
    "open_seats",
    "fill_rate",
    "total_registrations",
    "total_promotions",
    "avg_wait_to_confirm_seconds",
    "max_wait_to_confirm_seconds",
    "exported_at_utc",
)


# ---------------------------------------------------------------------------
# Row builders (pure functions -> easily unit-tested without touching disk)
# ---------------------------------------------------------------------------
def _manifest_row(reg: Registration, now: float) -> dict[str, Any]:
    return {
        "seat_number": reg.seat_number if reg.seat_number is not None else "",
        "reg_id": reg.reg_id,
        "attendee_id": reg.attendee.attendee_id,
        "name": reg.attendee.name,
        "email": reg.attendee.email,
        "tier": reg.attendee.tier,
        "status": reg.status.value,
        "registered_at_utc": to_iso(reg.registered_at),
        "confirmed_at_utc": to_iso(reg.confirmed_at),
        "wait_seconds": f"{reg.wait_time_seconds(now):.2f}",
        "promoted_from_waitlist": reg.promoted_from_waitlist,
        "queue_size_at_registration": reg.queue_size_at_registration
        if reg.queue_size_at_registration is not None
        else "",
        "note": reg.note,
    }


def manifest_rows(engine: SeatAllocationEngine, *, at: float | None = None) -> list[dict[str, Any]]:
    """One row per held seat (CONFIRMED / ATTENDED / NO_SHOW), seat-ordered."""
    now = engine._now(at)  # noqa: SLF001 - deliberate: keeps a single clock
    held = [
        r
        for r in engine.registrations
        if r.status
        in (
            RegistrationStatus.CONFIRMED,
            RegistrationStatus.ATTENDED,
            RegistrationStatus.NO_SHOW,
        )
    ]
    held.sort(key=lambda r: (r.seat_number is None, r.seat_number))
    return [_manifest_row(r, now) for r in held]


def waitlist_rows(engine: SeatAllocationEngine, *, at: float | None = None) -> list[dict[str, Any]]:
    """One row per waitlisted attendee, in live promotion order."""
    now = engine._now(at)  # noqa: SLF001
    rows: list[dict[str, Any]] = []
    for position, reg in enumerate(engine.waitlist(), start=1):
        rows.append(
            {
                "queue_position": position,
                "reg_id": reg.reg_id,
                "attendee_id": reg.attendee.attendee_id,
                "name": reg.attendee.name,
                "email": reg.attendee.email,
                "tier": reg.attendee.tier,
                "priority_weight": engine.config.tier_weight(reg.attendee.tier),
                "registered_at_utc": to_iso(reg.registered_at),
                "waiting_seconds": f"{max(0.0, now - reg.registered_at):.2f}",
                "seat_if_promoted": "",
            }
        )
    return rows


def audit_rows(engine: SeatAllocationEngine) -> list[dict[str, Any]]:
    """Every registration ever seen, with its terminal or current status."""
    rows: list[dict[str, Any]] = []
    for reg in engine.registrations:
        rows.append(
            {
                "reg_id": reg.reg_id,
                "attendee_id": reg.attendee.attendee_id,
                "name": reg.attendee.name,
                "email": reg.attendee.email,
                "tier": reg.attendee.tier,
                "status": reg.status.value,
                "registered_at_utc": to_iso(reg.registered_at),
                "confirmed_at_utc": to_iso(reg.confirmed_at),
                "decided_at_utc": to_iso(reg.decided_at),
                "seat_number": reg.seat_number if reg.seat_number is not None else "",
                "note": reg.note,
            }
        )
    return rows


def summary_rows(engine: SeatAllocationEngine, *, at: float | None = None) -> list[dict[str, Any]]:
    now = engine._now(at)  # noqa: SLF001
    stats = engine.stats(at=now)
    row = {k: stats.get(k, "") for k in SUMMARY_COLUMNS}
    row["exported_at_utc"] = to_iso(now)
    return [row]


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
def _write(path: str | Path, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" is required by the csv module; utf-8 handles non-ASCII names.
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def export_manifest(engine: SeatAllocationEngine, path: str | Path, *, at: float | None = None) -> Path:
    """Write the attendee manifest — the artifact you hand to the venue desk."""
    return _write(path, MANIFEST_COLUMNS, manifest_rows(engine, at=at))


def export_waitlist(engine: SeatAllocationEngine, path: str | Path, *, at: float | None = None) -> Path:
    """Write the live waitlist with positions — the artifact you publish."""
    return _write(path, WAITLIST_COLUMNS, waitlist_rows(engine, at=at))


def export_audit(engine: SeatAllocationEngine, path: str | Path) -> Path:
    """Write the full registration audit (every status, including rejects)."""
    return _write(path, AUDIT_COLUMNS, audit_rows(engine))


def export_summary(engine: SeatAllocationEngine, path: str | Path, *, at: float | None = None) -> Path:
    """Write the one-row event summary used by dashboards / tracking sheets."""
    return _write(path, SUMMARY_COLUMNS, summary_rows(engine, at=at))


def export_all(
    engine: SeatAllocationEngine, directory: str | Path, *, at: float | None = None, stem: str | None = None
) -> dict[str, Path]:
    """Write every export into ``directory``; returns ``{kind: path}``."""
    directory = Path(directory)
    stem = stem or engine.config.event_id
    return {
        "manifest": export_manifest(engine, directory / f"{stem}_manifest.csv", at=at),
        "waitlist": export_waitlist(engine, directory / f"{stem}_waitlist.csv", at=at),
        "audit": export_audit(engine, directory / f"{stem}_audit.csv"),
        "summary": export_summary(engine, directory / f"{stem}_summary.csv", at=at),
    }


def render_manifest_csv(engine: SeatAllocationEngine, *, at: float | None = None) -> str:
    """Manifest as a string (no filesystem) — handy for tests and for APIs."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(MANIFEST_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for row in manifest_rows(engine, at=at):
        writer.writerow(row)
    return buffer.getvalue()


def manifest_fingerprint(engine: SeatAllocationEngine, *, at: float | None = None) -> str:
    """SHA-256 of the rendered manifest.

    Publish this next to the CSV: if an attendee disputes the list, re-running
    the engine on the same audit log must reproduce the *identical* hash.
    """
    return hashlib.sha256(render_manifest_csv(engine, at=at).encode("utf-8")).hexdigest()


def iter_csv(path: str | Path) -> Iterator[dict[str, str]]:
    """Read a CSV back as dicts (used by the round-trip tests)."""
    with Path(path).open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)
