"""seatalloc — Automated Event Seat Allocation Engine.

A dependency-free, deterministic Python library that manages event waitlists and
seat caps using **priority queues (binary heaps)**, and exports attendee
manifests to CSV.

Quick start
-----------
>>> from seatalloc import Attendee, EventConfig, SeatAllocationEngine
>>> engine = SeatAllocationEngine(EventConfig(event_id="DEVX", name="DevXJMI", capacity=2))
>>> a = engine.register(Attendee("A1", "Ayesha", "ayesha@example.com", "MEMBER"), registered_at=100.0)
>>> b = engine.register(Attendee("A2", "Bilal", "bilal@example.com", "GENERAL"), registered_at=101.0)
>>> c = engine.register(Attendee("A3", "Charan", "charan@example.com", "GENERAL"), registered_at=102.0)
>>> c.status.value
'WAITLISTED'
>>> _ = engine.cancel(a.reg_id, at=103.0)          # a seat frees up
>>> c.status.value                                  # next in line is promoted
'CONFIRMED'
>>> engine.open_seats()
0

Design notes live in ``docs/DESIGN.md``; the measured complexity analysis lives
in ``docs/COMPLEXITY.md`` and ``docs/BENCHMARKS.md``.
"""

from .csv_export import (
    export_all,
    export_audit,
    export_manifest,
    export_summary,
    export_waitlist,
    manifest_fingerprint,
    render_manifest_csv,
)
from .engine import SeatAllocationEngine
from .models import (
    Attendee,
    CapacityConflictError,
    DuplicateRegistrationError,
    EngineError,
    EventConfig,
    EventWindowError,
    PromotionMode,
    Registration,
    RegistrationStatus,
    StatusConflictError,
    UnknownRegistrationError,
    WaitlistFullError,
)
from .persistence import EventLog, read_events, replay, write_events
from .priority import LazyDeletionHeap

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # core
    "SeatAllocationEngine",
    "EventConfig",
    "Attendee",
    "Registration",
    "RegistrationStatus",
    "PromotionMode",
    "LazyDeletionHeap",
    # errors
    "EngineError",
    "DuplicateRegistrationError",
    "CapacityConflictError",
    "UnknownRegistrationError",
    "EventWindowError",
    "WaitlistFullError",
    "StatusConflictError",
    # csv
    "export_manifest",
    "export_waitlist",
    "export_audit",
    "export_summary",
    "export_all",
    "render_manifest_csv",
    "manifest_fingerprint",
    # persistence
    "EventLog",
    "read_events",
    "write_events",
    "replay",
]
