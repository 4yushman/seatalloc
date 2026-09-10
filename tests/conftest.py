"""Shared fixtures. Every test uses explicit timestamps — never the wall clock."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:  # allow running pytest without an install step
    sys.path.insert(0, str(SRC))

from seatalloc import Attendee, EventConfig, SeatAllocationEngine  # noqa: E402

BASE = 1_700_000_000.0


@pytest.fixture
def config() -> EventConfig:
    return EventConfig(event_id="TEST", name="Test Event", capacity=3)


@pytest.fixture
def engine(config: EventConfig) -> SeatAllocationEngine:
    return SeatAllocationEngine(config)


def make_attendee(i: int, tier: str = "GENERAL") -> Attendee:
    return Attendee(f"A{i:04d}", f"Attendee {i}", f"user{i}@example.com", tier)


def make_engine(capacity: int = 3, **kwargs) -> SeatAllocationEngine:
    return SeatAllocationEngine(
        EventConfig(event_id="TEST", name="Test Event", capacity=capacity, **kwargs)
    )
