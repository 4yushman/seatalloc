"""Domain model for the Automated Event Seat Allocation Engine.

Everything in this module is a plain, immutable-ish data carrier. All mutation
of engine state happens through :class:`seatalloc.engine.SeatAllocationEngine`
so that the allocation rules stay in exactly one place.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "RegistrationStatus",
    "PromotionMode",
    "Attendee",
    "Registration",
    "EventConfig",
    "EngineError",
    "DuplicateRegistrationError",
    "CapacityConflictError",
    "UnknownRegistrationError",
    "EventWindowError",
    "WaitlistFullError",
    "StatusConflictError",
]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class RegistrationStatus(StrEnum):
    """Lifecycle of a single registration request.

    A registration is *never* deleted from the engine: it only moves between
    these states. This keeps the CSV manifest and the audit log consistent and
    makes the whole engine replayable (see ``seatalloc.persistence``).
    """

    CONFIRMED = "CONFIRMED"      # holds a seat that counts against capacity
    WAITLISTED = "WAITLISTED"    # waiting in the priority heap
    OFFERED = "OFFERED"          # promoted; seat held until the offer deadline
    CANCELLED = "CANCELLED"      # attendee withdrew (before or after promotion)
    REJECTED = "REJECTED"        # never eligible (duplicate/closed/invalid)
    EXPIRED = "EXPIRED"          # promotion offer timed out -> seat recycled
    ATTENDED = "ATTENDED"        # optional post-event check-in state
    NO_SHOW = "NO_SHOW"          # optional post-event check-in state

    @property
    def occupies_seat(self) -> bool:
        """True when this status is a *live* claim on a seat (before the event)."""
        return self is RegistrationStatus.CONFIRMED

    @property
    def holds_seat(self) -> bool:
        """True when this registration has been assigned a seat number that has
        not been handed back to the pool.

        That includes the post-event states: an attendee who checked in (or was
        marked a no-show) did occupy a seat, and treating those rows as "free"
        again would (a) let a finished event promote people off the waitlist and
        (b) corrupt the free-seat pool accounting that ``reconcile()`` audits.
        """
        return self in (
            RegistrationStatus.CONFIRMED,
            RegistrationStatus.OFFERED,
            RegistrationStatus.ATTENDED,
            RegistrationStatus.NO_SHOW,
        )

    @property
    def is_terminal(self) -> bool:
        return self in (
            RegistrationStatus.CANCELLED,
            RegistrationStatus.REJECTED,
            RegistrationStatus.EXPIRED,
            RegistrationStatus.ATTENDED,
            RegistrationStatus.NO_SHOW,
        )

    @property
    def is_active_queue_member(self) -> bool:
        return self in (RegistrationStatus.WAITLISTED, RegistrationStatus.OFFERED)


class PromotionMode(StrEnum):
    """How a freed seat is handed to the next waitlisted attendee."""

    AUTO = "auto"    # immediately CONFIRM the next attendee (fast, no reply needed)
    OFFER = "offer"  # OFFER the seat with a TTL; recycle it if unanswered


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Attendee:
    """A person registering for an event.

    ``tier`` drives prioritisation. It is validated against
    :attr:`EventConfig.tier_weights` at registration time so a typo can never
    silently demote somebody to the lowest priority bucket.
    """

    attendee_id: str
    name: str
    email: str
    tier: str = "GENERAL"

    def __post_init__(self) -> None:  # pragma: no cover - trivial guards
        if not self.attendee_id:
            raise ValueError("attendee_id must be non-empty")
        if not self.name.strip():
            raise ValueError("attendee name must be non-empty")
        if not _EMAIL_RE.match(self.email):
            raise ValueError(f"invalid email address: {self.email!r}")


@dataclass(slots=True)
class Registration:
    """One registration request plus its current allocation state."""

    reg_id: str
    attendee: Attendee
    registered_at: float                  # epoch seconds (UTC)
    sequence: int                         # monotonic tie-breaker, never reused
    status: RegistrationStatus = RegistrationStatus.WAITLISTED
    confirmed_at: float | None = None
    offered_at: float | None = None
    offer_expires_at: float | None = None
    decided_at: float | None = None
    seat_number: int | None = None        # 1..capacity, assigned on CONFIRMED
    queue_size_at_registration: int | None = None  # how many were queued when they joined
    promoted_from_waitlist: bool = False          # arrived via the queue, not directly
    note: str = ""

    # -- convenience ------------------------------------------------------
    @property
    def tier(self) -> str:
        return self.attendee.tier

    @property
    def registered_at_iso(self) -> str:
        return to_iso(self.registered_at)

    def wait_time_seconds(self, now: float) -> float:
        """Seconds from registration to confirmation (or to ``now`` if pending)."""
        end = self.confirmed_at if self.confirmed_at is not None else now
        return max(0.0, end - self.registered_at)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view used by the audit log and the CLI."""
        return {
            "reg_id": self.reg_id,
            "attendee_id": self.attendee.attendee_id,
            "name": self.attendee.name,
            "email": self.attendee.email,
            "tier": self.attendee.tier,
            "registered_at": self.registered_at,
            "sequence": self.sequence,
            "status": self.status.value,
            "confirmed_at": self.confirmed_at,
            "offered_at": self.offered_at,
            "offer_expires_at": self.offer_expires_at,
            "decided_at": self.decided_at,
            "seat_number": self.seat_number,
            "queue_size_at_registration": self.queue_size_at_registration,
            "promoted_from_waitlist": self.promoted_from_waitlist,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Registration:
        return cls(
            reg_id=payload["reg_id"],
            attendee=Attendee(
                attendee_id=payload["attendee_id"],
                name=payload["name"],
                email=payload["email"],
                tier=payload.get("tier", "GENERAL"),
            ),
            registered_at=float(payload["registered_at"]),
            sequence=int(payload["sequence"]),
            status=RegistrationStatus(payload["status"]),
            confirmed_at=payload.get("confirmed_at"),
            offered_at=payload.get("offered_at"),
            offer_expires_at=payload.get("offer_expires_at"),
            decided_at=payload.get("decided_at"),
            seat_number=payload.get("seat_number"),
            queue_size_at_registration=payload.get("queue_size_at_registration"),
            promoted_from_waitlist=bool(payload.get("promoted_from_waitlist", False)),
            note=payload.get("note", ""),
        )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_TIER_WEIGHTS: dict[str, int] = {
    "ORGANIZER": 0,   # EB / core team running the event
    "VOLUNTEER": 1,   # student volunteers
    "SPEAKER": 2,
    "MEMBER": 3,      # SoarJMI members
    "GENERAL": 4,     # everyone else
}


@dataclass(slots=True)
class EventConfig:
    """Static rules for one event.

    Attributes
    ----------
    event_id, name
        Identifiers used in the CSV manifest and the audit log.
    capacity
        Hard cap on *held* seats (CONFIRMED + unexpired OFFERED).
    tier_weights
        Lower weight == higher priority. Ordering is
        ``(tier_weight, registered_at, sequence)`` so that within a tier the
        queue is strictly FIFO and ties on identical timestamps are broken by
        arrival sequence — the ordering is therefore **total**, which is what
        makes the heap deterministic (standard heaps are not stable).
    promotion_mode
        ``AUTO`` confirms the next attendee outright; ``OFFER`` gives them a
        time-boxed window to accept before the seat is recycled.
    offer_ttl_seconds
        Acceptance window used when ``promotion_mode == OFFER``.
    registration_opens_at / registration_closes_at
        Optional epoch-second window; requests outside it are REJECTED.
    max_waitlist_size
        Optional ceiling on the queue (bounded queues / back-pressure). ``None``
        means unbounded.
    """

    event_id: str
    name: str = "Untitled Event"
    capacity: int = 0
    tier_weights: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TIER_WEIGHTS))
    promotion_mode: PromotionMode = PromotionMode.AUTO
    offer_ttl_seconds: float = 900.0
    registration_opens_at: float | None = None
    registration_closes_at: float | None = None
    max_waitlist_size: int | None = None

    def __post_init__(self) -> None:
        if self.capacity < 0:
            raise ValueError("capacity must be >= 0")
        if not self.tier_weights:
            raise ValueError("tier_weights must not be empty")
        if self.promotion_mode is PromotionMode.OFFER and self.offer_ttl_seconds <= 0:
            raise ValueError("offer_ttl_seconds must be > 0 in OFFER mode")
        if (
            self.registration_opens_at is not None
            and self.registration_closes_at is not None
            and self.registration_closes_at < self.registration_opens_at
        ):
            raise ValueError("registration_closes_at must be >= registration_opens_at")

    def tier_weight(self, tier: str) -> int:
        """Priority weight for ``tier``; unknown tiers sort last."""
        return self.tier_weights.get(tier, max(self.tier_weights.values()) + 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "name": self.name,
            "capacity": self.capacity,
            "tier_weights": dict(self.tier_weights),
            "promotion_mode": self.promotion_mode.value,
            "offer_ttl_seconds": self.offer_ttl_seconds,
            "registration_opens_at": self.registration_opens_at,
            "registration_closes_at": self.registration_closes_at,
            "max_waitlist_size": self.max_waitlist_size,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EventConfig:
        return cls(
            event_id=payload["event_id"],
            name=payload.get("name", "Untitled Event"),
            capacity=int(payload.get("capacity", 0)),
            tier_weights=dict(payload.get("tier_weights") or DEFAULT_TIER_WEIGHTS),
            promotion_mode=PromotionMode(payload.get("promotion_mode", "auto")),
            offer_ttl_seconds=float(payload.get("offer_ttl_seconds", 900.0)),
            registration_opens_at=payload.get("registration_opens_at"),
            registration_closes_at=payload.get("registration_closes_at"),
            max_waitlist_size=payload.get("max_waitlist_size"),
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class EngineError(Exception):
    """Base class for every error raised by the allocation engine."""


class DuplicateRegistrationError(EngineError):
    """The same attendee tried to register twice for the same event."""

    def __init__(self, attendee_id: str, existing_reg_id: str) -> None:
        super().__init__(
            f"attendee {attendee_id!r} is already registered as {existing_reg_id!r}"
        )
        self.attendee_id = attendee_id
        self.existing_reg_id = existing_reg_id


class CapacityConflictError(EngineError):
    """A capacity change would invalidate already-held seats."""


class UnknownRegistrationError(EngineError):
    """Operation referenced a registration id the engine has never seen."""

    def __init__(self, reg_id: str) -> None:
        super().__init__(f"unknown registration id: {reg_id!r}")
        self.reg_id = reg_id


class EventWindowError(EngineError):
    """Registration was attempted outside the configured window."""


class WaitlistFullError(EngineError):
    """The bounded waitlist is at ``max_waitlist_size``."""


class StatusConflictError(EngineError):
    """The requested transition is not legal from the current status."""

    def __init__(self, reg_id: str, current: str, attempted: str) -> None:
        super().__init__(
            f"cannot {attempted!r} registration {reg_id!r} in status {current!r}"
        )
        self.reg_id = reg_id
        self.current = current
        self.attempted = attempted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def to_iso(epoch_seconds: float | None) -> str:
    """Epoch seconds -> ISO-8601 UTC string (empty string for ``None``)."""
    if epoch_seconds is None:
        return ""
    return (
        datetime.fromtimestamp(epoch_seconds, tz=UTC)
        .isoformat(timespec="seconds")
    )
