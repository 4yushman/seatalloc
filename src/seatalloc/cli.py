"""Command-line interface: ``python -m seatalloc <command>``.

Commands
--------
``demo``        scripted, reproducible walkthrough of a real event (start here)
``simulate``    synthetic workload -> manifests, audit log, stats
``import-csv``  ingest registrations from a CSV and emit the manifest
``verify``      replay an audit log and prove the manifest is reproducible
``bench``       regenerate the empirical benchmark report
``stats``       print the operational snapshot for a rebuilt audit log

The CLI is intentionally boring: it parses arguments, calls the library, writes
files and prints text. All logic lives in the library modules.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .csv_export import (
    export_all,
    manifest_fingerprint,
)
from .engine import SeatAllocationEngine
from .models import (
    Attendee,
    EngineError,
    EventConfig,
    PromotionMode,
    RegistrationStatus,
    to_iso,
)
from .persistence import EventLog, replay

__all__ = ["main", "build_parser"]

DEMO_BASE = 1_762_000_000.0  # fixed epoch -> byte-identical demo output every run


# ---------------------------------------------------------------------------
# Formatting helpers (no third-party deps)
# ---------------------------------------------------------------------------
def h1(text: str) -> str:
    return f"\n\033[1m{text}\033[0m\n" + "─" * min(len(text) + 4, 88)


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    rows = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))
    sep = "  ".join("-" * w for w in widths)
    body = "\n".join("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) for row in rows)
    return f"{line}\n{sep}\n{body}"


def parse_when(value: str) -> float:
    """Accept epoch seconds or an ISO-8601 timestamp."""
    try:
        return float(value)
    except ValueError:
        pass
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------
def cmd_demo(args: argparse.Namespace) -> int:
    """A minute-long story that exercises every code path, deterministically."""
    out = Path(args.out)
    config = EventConfig(
        event_id="DEVX26",
        name="DevXJMI 2026 — Building with LLM APIs",
        capacity=args.capacity,
        promotion_mode=PromotionMode(args.mode),
        offer_ttl_seconds=600.0,
    )
    log_path = out / "devx26_audit.jsonl"

    with EventLog(log_path, config) as log:
        engine = SeatAllocationEngine(config, audit_hook=log.append)

        print(h1("1. Event opens"))
        print(f"   {config.name}\n   capacity={config.capacity}  mode={config.promotion_mode.value}")

        # --- arrivals: deliberately out of order in time -----------------
        script = [
            ("A-101", "Ayesha Khan", "ayesha@example.com", "MEMBER", 10.0),
            ("A-102", "Bilal Ahmed", "bilal@example.com", "GENERAL", 20.0),
            ("A-103", "Charan Das", "charan@example.com", "GENERAL", 30.0),
            ("A-104", "Devika Rao", "devika@example.com", "VOLUNTEER", 40.0),
            ("A-105", "Emaan Sheikh", "emaan@example.com", "MEMBER", 50.0),
            ("A-106", "Farhan Qureshi", "farhan@example.com", "GENERAL", 60.0),
            ("A-107", "Gauri Menon", "gauri@example.com", "GENERAL", 70.0),
        ]
        print(h1("2. Registrations arrive"))
        for attendee_id, name, email, tier, offset in script:
            reg = engine.register(
                Attendee(attendee_id, name, email, tier), registered_at=DEMO_BASE + offset
            )
            label = "CONFIRMED" if reg.status is RegistrationStatus.CONFIRMED else "WAITLISTED"
            seat = reg.seat_number if reg.seat_number is not None else "-"
            print(f"   {name:<18} {tier:<10} -> {label:<10} seat={seat}")

        print(h1("3. Waitlist (promotion order)"))
        print(
            table(
                ["#", "name", "tier", "registered_at", "waiting"],
                [
                    (
                        i,
                        r.attendee.name,
                        r.tier,
                        to_iso(r.registered_at),
                        f"{DEMO_BASE + 700 - r.registered_at:.0f}s",
                    )
                    for i, r in enumerate(engine.waitlist(), start=1)
                ],
            )
        )

        print(h1("4. A confirmed attendee cancels -> one promotion"))
        first = engine.confirmed()[0]
        print(f"   {first.attendee.name} cancels (seat {first.seat_number} recycled)")
        engine.cancel(first.reg_id, at=DEMO_BASE + 800)
        for r in engine.registrations:
            if r.promoted_from_waitlist and r.status.holds_seat:
                print(
                    f"   -> promoted from waitlist: {r.attendee.name} is now "
                    f"{r.status.value} holding seat {r.seat_number}"
                )

        print(h1("5. Organiser doubles the capacity"))
        promoted = engine.set_capacity(args.capacity * 2, at=DEMO_BASE + 900)
        print(f"   capacity -> {engine.capacity}; {promoted} attendee(s) pulled off the waitlist")
        print(f"   open seats now: {engine.open_seats()}   waitlist: {engine.waitlist_size()}")

        print(h1("6. Someone declines a promotion (offer TTL 600s)"))
        if engine.offers():
            declined = engine.offers()[0]
            engine.decline_offer(declined.reg_id, at=DEMO_BASE + 950)
            print(f"   {declined.attendee.name} declined; seat passed to next in line")

        print(h1("7. Operational snapshot"))
        stats = engine.stats(at=DEMO_BASE + 1000)
        for key in (
            "capacity", "confirmed", "offers_outstanding", "waitlisted", "cancelled",
            "open_seats", "fill_rate", "total_promotions", "tombstone_debt",
        ):
            print(f"   {key:<22} {stats[key]}")

        print(h1("8. Exports"))
        paths = export_all(engine, out, at=DEMO_BASE + 1000, stem="devx26")
        for kind, path in paths.items():
            print(f"   {kind:<9} {path}")
        fingerprint = manifest_fingerprint(engine, at=DEMO_BASE + 1000)
        print(f"\n   manifest SHA-256: {fingerprint}")
        print(f"   audit log:        {log_path}")

    print(h1("9. Replay the audit log (proves the run is reproducible)"))
    rebuilt = replay(log_path)
    print(f"   rebuilt engine:   {rebuilt!r}")
    print(f"   reconcile():      {rebuilt.reconcile() or 'no violations'}")
    print(f"   manifest matches: {manifest_fingerprint(rebuilt, at=DEMO_BASE + 1000) == fingerprint}")
    return 0


# ---------------------------------------------------------------------------
# simulate
# ---------------------------------------------------------------------------
def cmd_simulate(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    out = Path(args.out)
    config = EventConfig(
        event_id=args.event_id,
        name=args.name,
        capacity=args.capacity,
        promotion_mode=PromotionMode(args.mode),
        offer_ttl_seconds=args.offer_ttl,
    )
    log_path = out / "audit.jsonl"
    base = DEMO_BASE
    tiers = ["GENERAL", "GENERAL", "MEMBER", "VOLUNTEER", "ORGANIZER"]

    with EventLog(log_path, config) as log:
        engine = SeatAllocationEngine(config, audit_hook=log.append)
        for i in range(args.attendees):
            attendee = Attendee(
                attendee_id=f"SIM{i:06d}",
                name=f"Attendee {i}",
                email=f"attendee{i}@example.com",
                tier=rng.choices(tiers, weights=[50, 20, 20, 8, 2])[0],
            )
            engine.register(attendee, registered_at=base + i * args.arrival_gap)

        # churn: cancellations spread across the registration window
        confirmed_ids = [r.reg_id for r in engine.confirmed()]
        rng.shuffle(confirmed_ids)
        n_cancel = int(len(confirmed_ids) * args.cancel_rate)
        for i, reg_id in enumerate(confirmed_ids[:n_cancel]):
            engine.cancel(reg_id, at=base + args.attendees * args.arrival_gap + i)

        if args.grow_capacity:
            engine.set_capacity(int(args.capacity * args.grow_capacity),
                                at=base + args.attendees * args.arrival_gap + n_cancel + 1)

        if args.mode == "offer":
            engine.expire_offers(at=base + args.attendees * args.arrival_gap + n_cancel + args.offer_ttl + 1)

        end = base + args.attendees * args.arrival_gap + n_cancel + args.offer_ttl + 2
        paths = export_all(engine, out, at=end, stem=args.event_id.lower())
        stats = engine.stats(at=end)

    print(h1(f"Simulation: {args.attendees:,} registrations, capacity {args.capacity:,}"))
    print(
        table(
            ["metric", "value"],
            [(k, v) for k, v in stats.items() if k not in ("event_id",)],
        )
    )
    print(f"\n   manifest:      {paths['manifest']}")
    print(f"   waitlist:      {paths['waitlist']}")
    print(f"   audit csv:     {paths['audit']}")
    print(f"   summary:       {paths['summary']}")
    print(f"   audit jsonl:   {log_path}")
    print(f"   SHA-256:       {manifest_fingerprint(engine, at=end)}")
    violations = engine.reconcile()
    print(f"   reconcile():   {violations or 'no violations'}")
    return 1 if violations else 0


# ---------------------------------------------------------------------------
# import-csv
# ---------------------------------------------------------------------------
def cmd_import_csv(args: argparse.Namespace) -> int:
    import csv as _csv

    source = Path(args.input)
    out = Path(args.out)
    config = EventConfig(
        event_id=args.event_id,
        name=args.name,
        capacity=args.capacity,
        promotion_mode=PromotionMode(args.mode),
        max_waitlist_size=args.max_waitlist,
    )
    log_path = out / "audit.jsonl"
    reasons: dict[str, int] = {}
    snapshot_at = DEMO_BASE  # newest timestamp seen; keeps waits in CSV meaningful

    with EventLog(log_path, config) as log:
        engine = SeatAllocationEngine(config, audit_hook=log.append)
        with source.open(newline="", encoding="utf-8") as fh:
            reader = _csv.DictReader(fh)
            for row_number, row in enumerate(reader, start=2):
                try:
                    attendee = Attendee(
                        attendee_id=(row.get("attendee_id") or f"ROW{row_number}").strip(),
                        name=(row.get("name") or "").strip(),
                        email=(row.get("email") or "").strip(),
                        tier=(row.get("tier") or "GENERAL").strip().upper(),
                    )
                except ValueError as exc:
                    reasons["invalid_row"] = reasons.get("invalid_row", 0) + 1
                    print(f"   row {row_number}: skipped ({exc})", file=sys.stderr)
                    continue
                when = row.get("registered_at") or ""
                try:
                    at = parse_when(when) if when else snapshot_at + row_number
                except ValueError:
                    reasons["invalid_timestamp"] = reasons.get("invalid_timestamp", 0) + 1
                    print(f"   row {row_number}: skipped (unparseable registered_at {when!r})",
                          file=sys.stderr)
                    continue
                snapshot_at = max(snapshot_at, at)
                _, reason = engine.try_register(attendee, registered_at=at)
                if reason:
                    reasons[reason] = reasons.get(reason, 0) + 1

        # Export at the newest timestamp in the file (not the wall clock) so
        # wait_seconds reflects the event, not the machine running the import.
        paths = export_all(engine, out, at=snapshot_at, stem=args.event_id.lower())
        stats = engine.stats(at=snapshot_at)

    print(h1(f"Imported {source.name} -> {args.event_id}"))
    print(table(["metric", "value"], [(k, v) for k, v in stats.items() if k != "event_id"]))
    if reasons:
        print("\n   rejected rows:")
        for reason, count in sorted(reasons.items()):
            print(f"     {reason:<16} {count}")
    print(f"\n   manifest: {paths['manifest']}")
    print(f"   waitlist: {paths['waitlist']}")
    violations = engine.reconcile()
    print(f"   reconcile(): {violations or 'no violations'}")
    return 1 if violations else 0


# ---------------------------------------------------------------------------
# verify / stats / bench
# ---------------------------------------------------------------------------
def cmd_verify(args: argparse.Namespace) -> int:
    engine = replay(args.log)
    violations = engine.reconcile()
    print(h1(f"Replayed {args.log}"))
    print(f"   {engine!r}")
    print(f"   fingerprint: {manifest_fingerprint(engine)}")
    print(f"   reconcile(): {violations or 'no violations'}")
    if args.expect:
        match = manifest_fingerprint(engine) == args.expect
        print(f"   expected:    {args.expect}\n   matches:     {match}")
        return 0 if match and not violations else 1
    return 1 if violations else 0


def cmd_stats(args: argparse.Namespace) -> int:
    engine = replay(args.log)
    stats = engine.stats()
    print(h1("Operational snapshot"))
    print(table(["metric", "value"], list(stats.items())))
    print("\nWaitlist order:")
    print(
        table(
            ["#", "name", "tier", "registered_at"],
            [(i, r.attendee.name, r.tier, to_iso(r.registered_at))
             for i, r in enumerate(engine.waitlist()[: args.limit], start=1)],
        )
    )
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from .benchmarks import render_markdown, run_all

    report = run_all()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(report), encoding="utf-8")
    Path("benchmarks").mkdir(exist_ok=True)
    Path("benchmarks/results.json").write_text(report.to_json(), encoding="utf-8")
    print(render_markdown(report))
    print(f"\nWrote {out} and benchmarks/results.json")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Rebuild from a log and re-emit CSVs at a snapshot timestamp."""
    engine = replay(args.log)
    end = parse_when(args.at) if args.at else None
    paths = export_all(engine, args.out, at=end, stem=args.stem or engine.config.event_id.lower())
    print(json.dumps({k: str(v) for k, v in paths.items()}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seatalloc",
        description="Automated Event Seat Allocation Engine (heap-based waitlists + CSV manifests).",
    )
    parser.add_argument("--version", action="version", version="seatalloc 1.0.0")
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="reproducible walkthrough of a real event")
    p_demo.add_argument("--capacity", type=int, default=4)
    p_demo.add_argument("--mode", choices=["auto", "offer"], default="offer")
    p_demo.add_argument("--out", default="data/demo")
    p_demo.set_defaults(func=cmd_demo)

    p_sim = sub.add_parser("simulate", help="synthetic workload -> CSVs + audit log")
    p_sim.add_argument("--attendees", type=int, default=5_000)
    p_sim.add_argument("--capacity", type=int, default=1_000)
    p_sim.add_argument("--cancel-rate", type=float, default=0.15)
    p_sim.add_argument("--arrival-gap", type=float, default=2.0)
    p_sim.add_argument("--mode", choices=["auto", "offer"], default="auto")
    p_sim.add_argument("--offer-ttl", type=float, default=600.0)
    p_sim.add_argument("--grow-capacity", type=float, default=0.0,
                       help="multiply capacity by this factor partway through (e.g. 1.5)")
    p_sim.add_argument("--seed", type=int, default=42)
    p_sim.add_argument("--event-id", default="SIM")
    p_sim.add_argument("--name", default="Simulated Event")
    p_sim.add_argument("--out", default="data/sim")
    p_sim.set_defaults(func=cmd_simulate)

    p_imp = sub.add_parser("import-csv", help="ingest a registrations CSV, emit manifest")
    p_imp.add_argument("input", help="CSV with columns attendee_id,name,email,tier,registered_at")
    p_imp.add_argument("--capacity", type=int, required=True)
    p_imp.add_argument("--event-id", default="IMPORT")
    p_imp.add_argument("--name", default="Imported Event")
    p_imp.add_argument("--mode", choices=["auto", "offer"], default="auto")
    p_imp.add_argument("--max-waitlist", type=int, default=None)
    p_imp.add_argument("--out", default="data/import")
    p_imp.set_defaults(func=cmd_import_csv)

    p_ver = sub.add_parser("verify", help="replay an audit log and check invariants")
    p_ver.add_argument("log")
    p_ver.add_argument("--expect", default=None, help="expected manifest SHA-256")
    p_ver.set_defaults(func=cmd_verify)

    p_stat = sub.add_parser("stats", help="operational snapshot from an audit log")
    p_stat.add_argument("log")
    p_stat.add_argument("--limit", type=int, default=10)
    p_stat.set_defaults(func=cmd_stats)

    p_exp = sub.add_parser("export", help="re-emit CSVs from an audit log")
    p_exp.add_argument("log")
    p_exp.add_argument("--out", default="data/export")
    p_exp.add_argument("--stem", default=None)
    p_exp.add_argument("--at", default=None, help="snapshot epoch/ISO time")
    p_exp.set_defaults(func=cmd_export)

    p_bench = sub.add_parser("bench", help="regenerate the benchmark report")
    p_bench.add_argument("--out", default="docs/BENCHMARKS.md")
    p_bench.set_defaults(func=cmd_bench)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except EngineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: file not found: {exc.filename}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
