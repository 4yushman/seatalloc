"""Append-only JSONL audit log + deterministic replay.

Why bother?
-----------
A seat allocation is a *disputed-resource* decision: somebody will ask "why did
Rohan get seat 12 and not me?". A mutable database row cannot answer that; an
append-only event log can. Every state change is one JSON object per line, so:

* ``tail -f`` gives you a live feed of the event,
* ``replay()`` rebuilds the exact engine state from the log — no database,
  no migrations, no drift between "what the CSV says" and "what happened",
* the log is the source of truth for the ``reconcile()`` / fingerprint checks.

The format is deliberately boring (JSONL, one event per line, no schema
registry) because it must survive being copied into a Google Sheet, a GitHub
gist or a ``.txt`` file on someone's laptop.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .engine import SeatAllocationEngine
from .models import Attendee, EventConfig

__all__ = ["EventLog", "read_events", "replay", "write_events"]

_HEADER_OP = "event_created"
#: Events that mutate state and therefore must be replayed. Everything else
#: (confirm_seat, offer_seat, waitlist_push, cancel_noop, expire_offer) is
#: *derived* detail that replay reproduces as a side effect.
PRIMARY_OPS: frozenset[str] = frozenset(
    {
        "register",
        "reject",
        "cancel",
        "accept_offer",
        "decline_offer",
        "expire_offers",
        "set_capacity",
        "reinstate",
        "check_in",
        "mark_no_show",
    }
)


class EventLog:
    """A line-buffered JSONL writer you hand to :class:`SeatAllocationEngine`.

    Usage::

        with EventLog("data/demo.jsonl", engine.config) as log:
            engine = SeatAllocationEngine(config, audit_hook=log.append)
    """

    __slots__ = ("path", "_fh", "_closed")

    def __init__(self, path: str | Path, config: EventConfig, *, truncate: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w" if truncate else "a", encoding="utf-8")
        self._closed = False
        if truncate:
            # The header is what makes the log self-describing: replay() needs
            # no extra arguments to rebuild the event's configuration.
            self.append({**_HEADER_OP_KEYS, "op": _HEADER_OP, "at": 0.0, "config": config.to_dict()})

    def append(self, event: dict[str, Any]) -> None:
        if self._closed:  # pragma: no cover - defensive
            raise ValueError("EventLog is closed")
        self._fh.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        if not self._closed:
            self._fh.close()
            self._closed = True

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_HEADER_OP_KEYS: dict[str, Any] = {"sequence": -1}


# ---------------------------------------------------------------------------
# Reading / replaying
# ---------------------------------------------------------------------------
def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Parse a JSONL audit log into a list of events (blank lines ignored)."""
    events: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:  # pragma: no cover - corrupt file
                raise ValueError(f"{path}:{line_number}: invalid JSON ({exc})") from exc
    return events


def iter_events(path: str | Path) -> Iterator[dict[str, Any]]:
    yield from read_events(path)


def write_events(path: str | Path, events: list[dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
    return path


def replay(path: str | Path) -> SeatAllocationEngine:
    """Rebuild engine state by re-applying the log, event by event.

    Determinism is what makes this valid: every primary event carries the
    timestamp it happened at, and the engine derives all ordering from
    ``(tier_weight, registered_at, sequence)`` — no wall clock, no randomness.

    Raises
    ------
    ValueError
        The log does not start with an ``event_created`` header.
    """
    events = read_events(path)
    if not events or events[0].get("op") != _HEADER_OP:
        raise ValueError(
            f"{path}: expected an {_HEADER_OP!r} header as the first event; "
            "re-run the exporter or check that the log was not truncated"
        )
    config = EventConfig.from_dict(events[0]["config"])
    engine = SeatAllocationEngine(config)

    for event in events[1:]:
        op = event.get("op")
        if op not in PRIMARY_OPS:
            continue  # derived detail event — reproduced implicitly
        at = float(event["at"])
        if op in ("register", "reject"):
            attendee = Attendee(
                attendee_id=event["attendee_id"],
                name=event["name"],
                email=event["email"],
                tier=event.get("tier", "GENERAL"),
            )
            if op == "register":
                engine.register(attendee, registered_at=at)
            else:
                engine.try_register(attendee, registered_at=at)
        elif op == "cancel":
            engine.cancel(event["reg_id"], at=at, reason=event.get("reason", "cancelled"))
        elif op == "accept_offer":
            engine.accept_offer(event["reg_id"], at=at)
        elif op == "decline_offer":
            engine.decline_offer(event["reg_id"], at=at, reason=event.get("reason", "declined"))
        elif op == "expire_offers":
            engine.expire_offers(at=at)
        elif op == "set_capacity":
            engine.set_capacity(int(event["new"]), at=at)
        elif op == "reinstate":
            engine.reinstate(event["reg_id"], at=at)
        elif op == "check_in":
            engine.check_in(event["reg_id"], at=at)
        elif op == "mark_no_show":
            engine.mark_no_show(event["reg_id"], at=at)
    return engine
