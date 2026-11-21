"""Pickup (pre-carriage): German postcode -> zone -> airport, trucker requests, bid parsing."""

from __future__ import annotations

import csv
import logging
import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .extract import EMAIL_ANY_RE, Mail, Shipment, parse_number

log = logging.getLogger(__name__)

ZIP_RE = re.compile(
    r"(?<![\d\-/.,])(?P<zip>\d{5})(?![\d\-/.,]|\s*(?i:kg|kgs|lbs?|cbm|m3|eur|€|usd|pcs))"
    r"(?:\s+(?P<city>[A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\-]+(?:[ \-][A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\-]+){0,2}))?"
)
PICKUP_CTX_RE = re.compile(
    r"(?i)(pick[\s-]?up|collect(?:ion)?|abhol\w*|ab\s+werk|ex\s+works|from|shipper|warehouse"
    r"|lager|address|adresse|located|loading|ladestelle|origin)"
)
DELIVERY_CTX_RE = re.compile(r"(?i)(deliver\w*|consignee|empfänger|zustell\w*|to)\s*[:\-]?\s*$")
DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b|\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b")
FASTEST_RE = re.compile(
    r"(?i)\b(fastest|quickest|urgent(?:ly)?|express|asap|as\s+soon\s+as\s+possible|time[\s-]critical"
    r"|shortest\s+transit|schnellst\w*|eilig|dringend|express)\b"
)
MONEY_RE = re.compile(r"(?:(?:EUR|€)\s*(?P<a>\d[\d.,']*)|(?P<b>\d[\d.,']*)\s*(?:EUR|€|euros?)\b)", re.I)
TOTAL_LABEL_RE = re.compile(r"(?i)(total|all[\s-]?in|gesamt|insgesamt|pauschal|flat|price|preis|rate)\W{0,20}$")


@dataclass
class Trucker:
    name: str
    email: str
    zones: set[str]


@dataclass
class PickupInfo:
    zip: str
    city: str
    zone: str
    airport: str
    date: str | None


@dataclass
class PickupRequest:
    """One customer RFQ waiting for trucker bids."""

    mail: Mail
    ex: Any  # Extraction
    lots: list
    info: PickupInfo
    deadline: float  # monotonic, retries/replies must finish before
    window_end: float  # monotonic end of 6-min window
    wait_until: float  # monotonic: finalize at the latest here
    t0: float
    asked: dict[str, str] = field(default_factory=dict)  # email -> trucker name
    sent_ids: set[int] = field(default_factory=set)
    bids: dict[str, Decimal | None] = field(default_factory=dict)  # email -> price (None = declined)

    @property
    def complete(self) -> bool:
        return bool(self.asked) and set(self.bids) >= set(self.asked)


def _detect_date(text: str) -> str | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    if m.group(1):
        return m.group(1)
    d, mo, y = m.group(2), m.group(3), m.group(4)
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def is_fastest(text: str) -> bool:
    return bool(FASTEST_RE.search(text))


def parse_price(text: str) -> tuple[Decimal | None, bool]:
    """(price, ambiguous). Prefers amounts labelled total/all-in."""
    found: list[tuple[float, bool]] = []
    for m in MONEY_RE.finditer(text):
        v = parse_number(m["a"] or m["b"], thousands_3digit=True)
        if v is None or v <= 0:
            continue
        labelled = bool(TOTAL_LABEL_RE.search(text[max(0, m.start() - 40) : m.start()]))
        found.append((v, labelled))
    if not found:
        return None, False
    labelled = [v for v, lab in found if lab]
    pool = sorted(set(labelled or [v for v, _ in found]))
    if len(pool) == 1:
        return Decimal(str(pool[0])), False
    return Decimal(str(max(pool))), True


def ref_ids(mail: Mail) -> set[int]:
    """Message ids this mail replies to (field names not pinned down: try several)."""
    out: set[int] = set()
    for key in ("in_reply_to", "references", "reply_to_id", "reply_to", "parent_id"):
        v = mail.raw.get(key)
        for x in v if isinstance(v, list) else [v]:
            if isinstance(x, dict):
                x = x.get("id")
            try:
                out.add(int(x))
            except (TypeError, ValueError):
                pass
    return out


class PickupDesk:
    def __init__(self, zones_path: str, carriers_path: str):
        self.zones: dict[str, tuple[str, str]] = {}  # "51" -> (area, airport)
        self.truckers: list[Trucker] = []
        if Path(zones_path).is_file():
            with open(zones_path, encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    z = (row.get("zone") or "").strip()
                    if z:
                        self.zones[z.zfill(2)] = ((row.get("area") or "").strip(), (row.get("airport") or "").strip().upper())
        else:
            log.warning("pickup zones %s not found: pickup disabled", zones_path)
        if Path(carriers_path).is_file():
            with open(carriers_path, encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if (row.get("kind") or "").strip().lower() == "trucker":
                        self.truckers.append(
                            Trucker(
                                row["name"].strip(),
                                row["email"].strip().lower(),
                                {z.zfill(2) for z in (row.get("serves") or "").split()},
                            )
                        )
        self.max_truckers = int(os.getenv("PICKUP_MAX_TRUCKERS", "5"))
        self.wait_s = float(os.getenv("PICKUP_WAIT_S", "150"))
        self.pending: list[PickupRequest] = []
        self.done: list[PickupRequest] = []  # finished, kept to recognise late bids
        log.info("pickup: %d zones, %d truckers", len(self.zones), len(self.truckers))

    # -- detection ---------------------------------------------------------------
    def detect(self, text: str) -> PickupInfo | None:
        if not self.zones:
            return None
        best: tuple[int, PickupInfo] | None = None
        for m in ZIP_RE.finditer(text):
            zone = m["zip"][:2]
            if zone not in self.zones:
                continue
            before = text[max(0, m.start() - 80) : m.start()]
            if DELIVERY_CTX_RE.search(before[-25:]):
                continue
            score = 2 if PICKUP_CTX_RE.search(before) else 1
            area, airport = self.zones[zone]
            info = PickupInfo(m["zip"], (m["city"] or area).strip(), zone, airport, _detect_date(text))
            if best is None or score > best[0]:
                best = (score, info)
        return best[1] if best else None

    def truckers_for(self, zone: str) -> list[Trucker]:
        return [t for t in self.truckers if zone in t.zones][: self.max_truckers]

    # -- requests ------------------------------------------------------------------
    def request_body(self, t: Trucker, info: PickupInfo, s: Shipment) -> str:
        sig = os.getenv("REPLY_SIGNATURE", "Air Freight Desk").strip()
        return "\n".join(
            [
                f"Hello {t.name},",
                "",
                "please quote the following pickup:",
                f"From: {info.zip} {info.city}",
                f"To: {info.airport} airport (air export warehouse)",
                f"Date: {info.date or 'asap'}",
                f"Pieces: {s.pieces if s.pieces else 'n/a'}",
                f"Weight: {s.weight_kg:g} kg" if s.weight_kg else "Weight: n/a",
                f"Volume: {s.volume_cbm:g} cbm" if s.volume_cbm else "Volume: n/a",
                "",
                "Please reply with your all-in price in EUR.",
                "",
                "Best regards,",
                sig,
            ]
        )

    def send_requests(self, client, req: PickupRequest, shipment: Shipment, writer=None) -> None:
        info = req.info
        subject = f"Pickup quote {info.zip} {info.city} to {info.airport}" + (f" on {info.date}" if info.date else "")
        truckers = self.truckers_for(info.zone)
        body_core = None
        if writer is not None and truckers:  # one LLM call for all truckers, greeting added per trucker
            draft = "\n".join(self.request_body(truckers[0], info, shipment).splitlines()[2:])
            must = [info.zip, info.airport] + ([f"{shipment.weight_kg:g}"] if shipment.weight_kg else [])
            text = writer.write("pickup quote request to a trucking company", draft, must, greeting=False)
            body_core = text if text != draft else None
        for t in truckers:
            body = f"Hello {t.name},\n\n{body_core}" if body_core else self.request_body(t, info, shipment)
            try:
                resp = client.send_message(t.email, subject, body, deadline=req.deadline)
            except Exception as exc:  # noqa: BLE001
                log.warning("pickup request to %s failed: %s", t.email, exc)
                continue
            req.asked[t.email] = t.name
            sid = resp.get("id") if isinstance(resp, dict) else None
            if isinstance(sid, int):
                req.sent_ids.add(sid)
        self.pending.append(req)

    # -- bids ----------------------------------------------------------------------
    def match(self, mail: Mail, include_done: bool = False) -> PickupRequest | None:
        refs = ref_ids(mail)
        pool = self.done if include_done else self.pending
        for req in reversed(pool):
            if refs & req.sent_ids:
                return req
        m = EMAIL_ANY_RE.search(mail.sender or "")
        sender = m.group(0).lower() if m else ""
        for req in reversed(pool):
            if sender in req.asked and (req.info.zip in mail.subject or not refs):
                return req
        return None

    def due(self) -> list[PickupRequest]:
        now = time.monotonic()
        ready = [r for r in self.pending if r.complete or now >= r.wait_until]
        self.pending = [r for r in self.pending if r not in ready]
        self.done = (self.done + ready)[-100:]
        return ready
