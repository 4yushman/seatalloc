"""Zero-dependency web demo for the seat allocation engine.

This is a *presentation layer* on top of the library — the engine itself still
has no third-party runtime dependencies, and this file uses nothing beyond the
Python standard library either, so the whole thing is one process with no build
step and no lockfile.

Run it locally
--------------
    python webapp/server.py            # http://localhost:8000
    PORT=9000 python webapp/server.py  # any port you like

Deploy it (Render / Railway / Fly / any container host)
-------------------------------------------------------
    start command:  python webapp/server.py
    $PORT is honoured automatically, and the server binds 0.0.0.0.

HTTP API (all JSON, all relative so it works behind any reverse proxy)
----------------------------------------------------------------------
    GET  /                        the single-page UI
    GET  /api/state               full snapshot used to render the UI
    POST /api/register            {"name": .., "email": .., "tier": ..}
    POST /api/cancel              {"reg_id": ..}
    POST /api/offer               {"reg_id": .., "accept": true|false}
    POST /api/expire              {} — sweep overdue offers, recycle seats
    POST /api/config              {"capacity": .., "mode": .., "ttl": ..}
    POST /api/reset               same body as /api/config
    POST /api/scenario            re-run the scripted 6-step demo story
    GET  /api/export/<kind>.csv   kind = manifest | waitlist | audit | summary

Every mutating endpoint returns ``{"ok": bool, "message": str, "state": {...}}``
so the browser never has to guess what happened.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # run straight from a clone, no install needed

from seatalloc import (  # noqa: E402
    Attendee,
    DuplicateRegistrationError,
    EngineError,
    EventConfig,
    PromotionMode,
    SeatAllocationEngine,
)
from seatalloc.csv_export import (  # noqa: E402
    AUDIT_COLUMNS,
    MANIFEST_COLUMNS,
    SUMMARY_COLUMNS,
    WAITLIST_COLUMNS,
    audit_rows,
    manifest_fingerprint,
    manifest_rows,
    summary_rows,
    waitlist_rows,
)

INDEX = Path(__file__).resolve().parent / "index.html"

TIERS = ("ORGANIZER", "VOLUNTEER", "SPEAKER", "MEMBER", "GENERAL")
AUDIT_TAIL = 14


def _short(value: Any) -> str:
    """Human-friendly rendering for the audit-log panel."""
    if isinstance(value, float) and value > 1e8:  # an epoch timestamp
        return time.strftime("%H:%M:%S", time.gmtime(value)) + "Z"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


# ---------------------------------------------------------------------------
# Demo state — one process-wide engine guarded by a lock
# ---------------------------------------------------------------------------
class Demo:
    """Owns the live engine and serialises every mutation."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.counter = 0
        self.reset(capacity=3, mode="offer", ttl=120.0)

    # -- lifecycle --------------------------------------------------------
    def reset(self, *, capacity: int = 3, mode: str = "offer", ttl: float = 120.0) -> str:
        self.counter = 0
        self.config = EventConfig(
            event_id="LIVE",
            name="DevXJMI Live Demo",
            capacity=max(0, int(capacity)),
            promotion_mode=PromotionMode(mode),
            offer_ttl_seconds=max(5.0, float(ttl)),
        )
        self.engine = SeatAllocationEngine(self.config)
        self.scenario_base = time.time()
        return f"Reset — capacity {self.config.capacity}, {self.config.promotion_mode.value.upper()} mode"

    # -- helpers ----------------------------------------------------------
    def _attendee(self, name: str, email: str, tier: str) -> Attendee:
        self.counter += 1
        return Attendee(f"LIVE-{self.counter:04d}", name, email, tier)

    def add(self, name: str, email: str, tier: str, *, at: float | None = None) -> str:
        """Register one attendee; returns a human-readable outcome."""
        name = (name or "").strip()
        email = (email or "").strip()
        if not name:
            raise ValueError("Please enter a name.")
        if "@" not in email or "." not in email.split("@")[-1]:
            # A real system would send a confirmation mail instead of guessing an
            # address; here we just keep the demo frictionless.
            handle = name.lower().replace(" ", ".")
            email = f"{handle}@example.com"
        if tier not in TIERS:
            tier = "GENERAL"

        attendee = self._attendee(name, email, tier)
        try:
            reg, reason = self.engine.try_register(attendee, registered_at=at)
        except EngineError as exc:  # pragma: no cover - defensive
            return f"Rejected: {exc}"
        except DuplicateRegistrationError:  # pragma: no cover - defensive
            return f"{name} is already registered."

        if reg is None:
            return f"{name} rejected ({reason})"
        if reg.status.value == "CONFIRMED":
            return f"✅ {name} confirmed — seat {reg.seat_number}"
        if reg.status.value == "OFFERED":
            ttl = self.config.offer_ttl_seconds
            return f"🔔 {name} offered seat {reg.seat_number} — accept within {ttl:.0f}s"
        return (
            f"🕐 {name} waitlisted at position {self.engine.waitlist_position(reg.reg_id)}"
            f" (tier {tier})"
        )

    # -- actions ----------------------------------------------------------
    def cancel(self, reg_id: str) -> str:
        reg = self.engine.get(reg_id)
        seat = reg.seat_number
        if not self.engine.cancel(reg_id):
            return f"{reg.attendee.name} was already cancelled"
        promoted = self.engine.waitlist_size()
        tail = f" — {promoted} still queued" if promoted else ""
        return f"Cancelled {reg.attendee.name} (seat {seat} released){tail}"

    def offer_decision(self, reg_id: str, accept: bool) -> str:
        reg = self.engine.get(reg_id)
        name = reg.attendee.name
        if accept:
            self.engine.accept_offer(reg_id)
            return f"✅ {name} accepted — seat {reg.seat_number} confirmed"
        self.engine.decline_offer(reg_id)
        return f"{name} declined — seat passed to the next in queue"

    def expire(self) -> str:
        expired = self.engine.expire_offers()
        if not expired:
            return "No offers were past their deadline"
        return f"⌛ {len(expired)} offer(s) expired — seats recycled to the queue"

    def configure(self, *, capacity: int, mode: str, ttl: float) -> str:
        self.config.promotion_mode = PromotionMode(mode)
        self.config.offer_ttl_seconds = max(5.0, float(ttl))
        promoted = self.engine.set_capacity(int(capacity))
        return (
            f"Capacity {self.config.capacity} · {self.config.promotion_mode.value.upper()} mode"
            + (f" · {promoted} promoted from the queue" if promoted else "")
        )

    def scenario(self) -> str:
        """The 6-step story a presenter can narrate out loud."""
        self.reset(capacity=3, mode="offer", ttl=120.0)
        base = time.time() - 240  # a few minutes ago, so wait times look real
        people = [
            ("Ayesha Khan", "MEMBER"),
            ("Bilal Ahmed", "GENERAL"),
            ("Charan Das", "GENERAL"),
            ("Devika Rao", "GENERAL"),
            ("Emaan Sheikh", "MEMBER"),
        ]
        for i, (name, tier) in enumerate(people):
            self.add(name, "", tier, at=base + i * 45)
        return "Scripted demo loaded — cancel a confirmed seat to see a promoted offer"

    # -- rendering --------------------------------------------------------
    def state(self) -> dict[str, Any]:
        now = time.time()
        engine = self.engine
        confirmed = []
        for seat, reg_id in engine.confirmed_seats():
            reg = engine.get(reg_id)
            confirmed.append(
                {
                    "reg_id": reg_id,
                    "seat_number": seat,
                    "name": reg.attendee.name,
                    "tier": reg.attendee.tier,
                    "status": reg.status.value,
                    "waited": reg.wait_time_seconds(now),
                    "promoted": reg.promoted_from_waitlist,
                    "cancellable": reg.status.value == "CONFIRMED",
                }
            )

        waitlist = []
        for position, reg in enumerate(engine.waitlist(), start=1):
            waitlist.append(
                {
                    "position": position,
                    "reg_id": reg.reg_id,
                    "name": reg.attendee.name,
                    "tier": reg.attendee.tier,
                    "priority_weight": engine.config.tier_weight(reg.attendee.tier),
                    "waiting": max(0.0, now - reg.registered_at),
                    "queued_behind": reg.queue_size_at_registration,
                }
            )

        offers = [
            {
                "reg_id": reg.reg_id,
                "name": reg.attendee.name,
                "tier": reg.attendee.tier,
                "seat_number": reg.seat_number,
                "expires_in": max(0.0, (reg.offer_expires_at or now) - now),
            }
            for reg in engine.offers()
        ]

        log = []
        for event in engine.audit_trail[-AUDIT_TAIL:]:
            detail = " ".join(
                f"{k}={_short(v)}"
                for k, v in event.items()
                if k not in {"op", "at", "sequence", "status"}
            )
            log.append({"op": event["op"], "detail": detail[:120]})
        log.reverse()

        return {
            "event": {
                "event_id": self.config.event_id,
                "name": self.config.name,
                "capacity": self.config.capacity,
                "promotion_mode": self.config.promotion_mode.value,
                "offer_ttl_seconds": self.config.offer_ttl_seconds,
                "tier_weights": dict(self.config.tier_weights),
            },
            "stats": engine.stats(at=now),
            "fingerprint": manifest_fingerprint(engine, at=now),
            "confirmed": confirmed,
            "waitlist": waitlist,
            "offers": offers,
            "log": log,
        }

    def csv_bytes(self, kind: str) -> tuple[str, bytes]:
        now = time.time()
        builders = {
            "manifest": (MANIFEST_COLUMNS, lambda: manifest_rows(self.engine, at=now)),
            "waitlist": (WAITLIST_COLUMNS, lambda: waitlist_rows(self.engine, at=now)),
            "audit": (AUDIT_COLUMNS, lambda: audit_rows(self.engine)),
            "summary": (SUMMARY_COLUMNS, lambda: summary_rows(self.engine, at=now)),
        }
        columns, build = builders[kind]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(build())
        return f"live_{kind}.csv", buffer.getvalue().encode("utf-8")


DEMO = Demo()


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "seatalloc-demo/1.0"

    # -- responses --------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _action(self, fn: Any, **kwargs: Any) -> None:
        try:
            with DEMO.lock:
                message = fn(**kwargs)
                state = DEMO.state()
            self._json({"ok": True, "message": message, "state": state})
        except EngineError as exc:
            with DEMO.lock:
                state = DEMO.state()
            self._json({"ok": False, "message": str(exc), "state": state})
        except Exception as exc:  # noqa: BLE001 - a demo must never 500 silently
            self._json({"ok": False, "message": f"{type(exc).__name__}: {exc}"}, status=200)

    # -- routing ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html = INDEX.read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/healthz":
            self._json({"ok": True, "uptime": round(time.time() - START_TIME, 1)})
            return
        if path == "/api/state":
            with DEMO.lock:
                self._json(DEMO.state())
            return
        if path.startswith("/api/export/"):
            kind = path.rsplit("/", 1)[-1]
            if kind.endswith(".csv"):
                kind = kind[:-4]
            if kind not in {"manifest", "waitlist", "audit", "summary"}:
                self._json({"ok": False, "message": f"unknown export {kind!r}"}, status=404)
                return
            with DEMO.lock:
                filename, payload = DEMO.csv_bytes(kind)
            self._send(
                200,
                payload,
                "text/csv; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="{filename}"'},
            )
            return
        self._json({"ok": False, "message": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "message": "invalid JSON body"}, status=400)
            return

        if path == "/api/register":
            self._action(DEMO.add, name=body.get("name", ""), email=body.get("email", ""),
                         tier=body.get("tier", "GENERAL"))
        elif path == "/api/cancel":
            self._action(DEMO.cancel, reg_id=body.get("reg_id", ""))
        elif path == "/api/offer":
            self._action(DEMO.offer_decision, reg_id=body.get("reg_id", ""),
                         accept=bool(body.get("accept")))
        elif path == "/api/expire":
            self._action(DEMO.expire)
        elif path in ("/api/config", "/api/reset"):
            limit = int(body.get("capacity", 3))
            mode = body.get("mode", "offer")
            ttl = float(body.get("ttl", 120))
            if path == "/api/reset":
                with DEMO.lock:
                    DEMO.reset(capacity=limit, mode=mode, ttl=ttl)
            self._action(DEMO.configure, capacity=limit, mode=mode, ttl=ttl)
        elif path == "/api/scenario":
            self._action(DEMO.scenario)
        else:
            self._json({"ok": False, "message": "not found"}, status=404)

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the deploy logs readable
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)


START_TIME = time.time()


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"seatalloc live demo → http://0.0.0.0:{port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
