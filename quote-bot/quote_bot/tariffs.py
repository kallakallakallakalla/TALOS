"""Tariff ingestion + pricing for airlines that send rate sheets (not bookable on the portal).

Parses rate sheets from plain text (mail body, txt, csv, pdf text, xlsx rows joined with ' | ',
docx tables, image transcriptions). Handles row tables (one lane per line), origin-grouped tables
(``Origin: CGN`` then destination rows), block layouts (one lane per block with Transit/Minimum/
from N kg lines), surcharges inline or in a separate section, decimal comma or point, ``-45/+45``
or ``from 45 kg`` or ``45 kg+`` break headers. Falls back to the LLM when no lane is found.

Price rule (identical in every start-kit sheet):
    chargeable = max(actual kg, cbm * 167)
    freight    = max(minimum, rate(break of billed kg) * billed kg)
    total      = freight + (fuel + security) * billed kg
    billed kg  = chargeable, or a higher break if that is cheaper
"""

from __future__ import annotations

import csv
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

NOT_CODES = {"EUR", "USD", "CBM", "KGS", "MIN", "MAX", "AND", "THE", "PER", "FOR", "LANE", "VIA", "ALL",
             "DAY", "NEW", "VAT", "AWB", "RFQ", "ETA", "ETD", "FCA", "EXW", "DAP", "DDP", "CPT", "FOB", "CIF"}
NUM = r"-?\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d+)?|-?\d+(?:[.,]\d+)?"
CODE = r"\b[A-Z]{3}\b"


def num(s: Any) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).replace("EUR", "").replace("€", "").strip()
    m = re.search(r"-?\d+(?:[.,]\d+)*", t)
    if not m:
        return None
    v = m.group(0)
    if "," in v and "." in v:  # 1.234,56 or 1,234.56
        v = v.replace(".", "").replace(",", ".") if v.rfind(",") > v.rfind(".") else v.replace(",", "")
    elif "," in v:
        v = v.replace(",", ".") if len(v.rsplit(",", 1)[1]) != 3 else v.replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None


@dataclass
class TariffLane:
    carrier: str
    origin: str
    destination: str
    minimum: float
    rates: dict[int, float]  # break kg -> EUR/kg
    fuel: float = 0.0
    security: float = 0.0
    transit_days: int | None = None
    currency: str = "EUR"
    valid_from: str | None = None
    valid_to: str | None = None
    carrier_email: str | None = None
    source: str = ""
    surcharges_found: bool = False

    def key(self) -> tuple[str, str, str]:
        return (self.carrier.lower(), self.origin, self.destination)


@dataclass
class ParseResult:
    carrier: str | None
    currency: str
    valid_from: str | None
    valid_to: str | None
    lanes: list[TariffLane] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# header fields
# ---------------------------------------------------------------------------
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS.update({"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "mai": 5, "juni": 6, "juli": 7, "oktober": 10,
               "dezember": 12, "okt": 10, "dez": 12})


def parse_date(s: str) -> str | None:
    s = (s or "").strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m:
        return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    m = re.search(r"(\d{1,2})\.?\s+([A-Za-zäÄ]+)\s+(\d{4})", s)
    if m and m[2].lower() in MONTHS:
        return f"{m[3]}-{MONTHS[m[2].lower()]:02d}-{int(m[1]):02d}"
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", s)
    if m and m[1].lower() in MONTHS:
        return f"{m[3]}-{MONTHS[m[1].lower()]:02d}-{int(m[2]):02d}"
    return None


def header_fields(text: str) -> tuple[str | None, str, str | None, str | None]:
    carrier = None
    for line in text.splitlines()[:15]:
        m = re.match(r"\s*(.+?)\s*[|–-]+\s*(?:air\s+)?(?:export\s+)?(?:tariff|rate\s*sheet|rates)\b", line, re.I)
        if m and len(m.group(1)) < 60:
            carrier = m.group(1).strip(" |")
            break
    cur = "EUR"
    m = re.search(r"(?i)currency\s*[|:]*\s*([A-Z]{3})", text)
    if m:
        cur = m.group(1).upper()
    vf = vt = None
    m = re.search(r"(?i)valid\s+from\s*[|:]*\s*([^\n|]+)", text)
    if m:
        vf = parse_date(m.group(1))
    m = re.search(r"(?i)valid\s+(?:to|until|till)\s*[|:]*\s*([^\n|]+)", text)
    if m:
        vt = parse_date(m.group(1))
    return carrier, cur, vf, vt


# ---------------------------------------------------------------------------
# lane + break detection
# ---------------------------------------------------------------------------
def lane_in(line: str, codes: set[str] | None = None) -> tuple[str, str, int] | None:
    """(origin, destination, end index of the lane text) if the line names a lane."""
    pats = [
        rf"\b([A-Z]{{3}})\s*[-–>/]\s*([A-Z]{{3}})\b",                          # CGN-SYD, CGN > SYD
        rf"\b([A-Z]{{3}})\b[^\n|]{{0,30}}?>\s*([A-Z]{{3}})\b",                # CGN Cologne > EZE Buenos Aires
        rf"\(([A-Z]{{3}})\)[^\n|]{{0,40}}?\bto\b[^\n|]{{0,40}}?\(([A-Z]{{3}})\)",  # Cologne (CGN) to Lagos (LOS)
        rf"\b([A-Z]{{3}})\s+to\s+([A-Z]{{3}})\b",                              # CGN to SYD
    ]
    for p in pats:
        m = re.search(p, line)
        if m and m.group(1) not in NOT_CODES and m.group(2) not in NOT_CODES and m.group(1) != m.group(2):
            if codes and (m.group(1) not in codes and m.group(2) not in codes):
                continue
            end = m.end()
            # include a trailing city name ("> EZE Buenos Aires") so its letters are not parsed
            tail = re.match(r"[A-Za-zÀ-ÿ .'()-]*", line[end:])
            return m.group(1), m.group(2), end + (len(tail.group(0)) if tail else 0)
    return None


def breaks_in(line: str) -> list[int] | None:
    """Weight breaks from a header line, e.g. '-45 +45 +100 ...', 'from 0 kg | from 45 kg', '0 kg+ 45 kg+'."""
    low = line.lower()
    toks: list[int] = []
    if re.search(r"(?<![\d.,])[-+]\s?\d{2,4}\b", line) and len(re.findall(r"(?<![\d.,])[-+]\s?\d{2,4}\b", line)) >= 3:
        for sign, n in re.findall(r"(?<![\d.,])([-+])\s?(\d{2,4})\b", line):
            toks.append(0 if sign == "-" else int(n))
    elif len(re.findall(r"(?i)\bfrom\s+\d+\s*kg", line)) >= 3:
        toks = [int(n) for n in re.findall(r"(?i)\bfrom\s+(\d+)\s*kg", line)]
    elif len(re.findall(r"\b\d+\s*kg\s*\+", line)) >= 3:
        toks = [int(n) for n in re.findall(r"\b(\d+)\s*kg\s*\+", line)]
    elif "kg" in low and len(re.findall(r"\b(?:min|minimum|n|q)?\s*\d+\s*kg\b", low)) >= 4:
        toks = [int(n) for n in re.findall(r"\b(\d+)\s*kg\b", low)]
    if len(toks) >= 3 and toks == sorted(toks) and len(set(toks)) == len(toks):
        if toks[0] != 0:
            toks = [0, *toks] if toks[0] == 45 and "-45" not in line else toks
        return toks
    return None


def numbers(s: str) -> list[str]:
    s = re.sub(r"\bD\+(\d+)", r"\1", s)               # transit D+3
    s = re.sub(r"(?i)(\d+)\s*days?\b", r"\1", s)        # 4 days
    s = re.sub(r"(?i)\b(?:EUR|USD|€)\s*", " ", s)
    return re.findall(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w])", s)


SUR_RE = re.compile(r"(?i)fuel\s*(?:surcharge)?\s*[:=]?\s*(?:EUR|€)?\s*(\d+(?:[.,]\d+)?)"
                    r".{0,40}?security\s*(?:surcharge)?\s*[:=]?\s*(?:EUR|€)?\s*(\d+(?:[.,]\d+)?)")


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def parse_tariff(text: str, carrier_hint: str | None = None, codes: set[str] | None = None) -> ParseResult:
    carrier, cur, vf, vt = header_fields(text)
    carrier = carrier_hint or carrier or "Unknown carrier"
    res = ParseResult(carrier, cur, vf, vt)
    lanes: dict[tuple[str, str], TariffLane] = {}
    sur: dict[tuple[str, str], tuple[float, float]] = {}
    lines = [ln.rstrip() for ln in text.replace("\t", " | ").splitlines()]
    breaks: list[int] | None = None
    origin_ctx: str | None = None
    in_sur = False
    block: dict | None = None

    def flush_block() -> None:
        nonlocal block
        if block and block.get("rates") and block.get("minimum") is not None:
            ks = sorted(block["rates"])
            lanes[(block["o"], block["d"])] = TariffLane(
                carrier, block["o"], block["d"], block["minimum"], {k: block["rates"][k] for k in ks},
                block.get("fuel") or 0.0, block.get("sec") or 0.0, block.get("transit"), cur, vf, vt,
                surcharges_found=block.get("fuel") is not None)
        block = None

    for raw in lines:
        line = raw.strip(" |")
        if not line:
            continue
        low = line.lower()
        if re.match(r"(?i)^surcharges?\b", line):
            flush_block()
            in_sur = True
            continue
        b = breaks_in(line)
        if b and not lane_in(line, codes):
            breaks = b
            continue
        m = re.match(r"(?i)^origin\s*:?\s*(.*)$", line)
        if m:
            flush_block()
            cs = [c for c in re.findall(CODE, m.group(1)) if c not in NOT_CODES]
            origin_ctx = cs[0] if cs else None
            in_sur = False
            continue

        # surcharge line "LANE: fuel X per kg, security Y per kg" (anywhere)
        sm = SUR_RE.search(line)
        ln = lane_in(line, codes)
        if sm and ln:
            sur[(ln[0], ln[1])] = (num(sm.group(1)), num(sm.group(2)))
            continue
        if in_sur and ln:  # surcharge table row: LANE fuel security
            ns = numbers(line[ln[2]:])
            if len(ns) >= 2:
                sur[(ln[0], ln[1])] = (num(ns[0]), num(ns[1]))
            continue

        # block layout: lane header alone on its line
        if ln and not numbers(line[ln[2]:]):
            flush_block()
            block = {"o": ln[0], "d": ln[1], "rates": {}}
            continue
        if block is not None:
            kv = re.match(r"(?i)^(transit|minimum|min\.?|fuel|security|from\s+(\d+)\s*kg|(\d+)\s*kg\s*\+|[-+]\s?(\d+))\s*[|:]*\s*(.*)$", line)
            if kv:
                key = kv.group(1).lower()
                val = numbers(kv.group(5))
                v = num(val[0]) if val else None
                if v is None:
                    continue
                if key.startswith("transit"):
                    block["transit"] = int(v)
                elif key.startswith("min"):
                    block["minimum"] = v
                elif key.startswith("fuel"):
                    block["fuel"] = v
                elif key.startswith("security"):
                    block["sec"] = v
                else:
                    brk = kv.group(2) or kv.group(3) or kv.group(4)
                    k = 0 if key.startswith("-") else int(brk)
                    block["rates"][k] = v
                continue
            if not ln:
                continue

        # row layout
        o = d = None
        rest = line
        if ln:
            o, d, end = ln
            rest = line[end:]
        elif origin_ctx:
            first = re.match(r"^\s*(?:[A-Za-zÀ-ÿ .'-]*\(([A-Z]{3})\)|([A-Z]{3})\b)", line)
            if first:
                code = first.group(1) or first.group(2)
                if code not in NOT_CODES:
                    o, d = origin_ctx, code
                    rest = line[first.end():]
        if not (o and d) or not breaks:
            continue
        ns = [num(x) for x in numbers(rest)]
        nb = len(breaks)
        transit = fuel = sec = None
        if len(ns) == nb + 4:
            transit, minimum, rates, fuel, sec = ns[0], ns[1], ns[2:2 + nb], ns[-2], ns[-1]
        elif len(ns) == nb + 2:
            transit, minimum, rates = ns[0], ns[1], ns[2:]
        elif len(ns) == nb + 1:
            minimum, rates = ns[0], ns[1:]
        elif len(ns) == nb + 3:  # minimum + rates + fuel + security, no transit
            minimum, rates, fuel, sec = ns[0], ns[1:1 + nb], ns[-2], ns[-1]
        else:
            res.notes.append(f"skipped {o}-{d}: {len(ns)} numbers for {nb} breaks")
            continue
        lanes[(o, d)] = TariffLane(
            carrier, o, d, minimum, dict(zip(breaks, rates)), fuel or 0.0, sec or 0.0,
            int(transit) if transit is not None else None, cur, vf, vt, surcharges_found=fuel is not None)
    flush_block()

    for k, (f, s) in sur.items():
        if k in lanes:
            lanes[k].fuel, lanes[k].security, lanes[k].surcharges_found = f or 0.0, s or 0.0, True
    for lane in lanes.values():
        if not lane.surcharges_found:
            res.notes.append(f"{lane.origin}-{lane.destination}: no surcharges found (assumed 0)")
    res.lanes = list(lanes.values())
    return res


LLM_TARIFF_PROMPT = (
    "Extract an air freight export tariff. JSON only: {\"carrier\":str|null,\"currency\":\"EUR\","
    "\"valid_from\":\"YYYY-MM-DD\"|null,\"valid_to\":\"YYYY-MM-DD\"|null,\"lanes\":[{\"origin\":\"IATA\","
    "\"destination\":\"IATA\",\"transit_days\":int|null,\"minimum\":number,\"fuel\":number,\"security\":number,"
    "\"rates\":{\"<break kg>\":number}}]}. Breaks: '-45' or 'from 0 kg' = 0. Rates per kg. Decimal comma = point."
)


def parse_llm_tariff(content: str, carrier_hint: str | None) -> ParseResult:
    data = json.loads(re.search(r"\{.*\}", content, re.S).group(0))
    res = ParseResult(carrier_hint or data.get("carrier") or "Unknown carrier", data.get("currency") or "EUR",
                      data.get("valid_from"), data.get("valid_to"))
    for x in data.get("lanes") or []:
        try:
            rates = {int(float(k)): float(v) for k, v in (x.get("rates") or {}).items()}
            res.lanes.append(TariffLane(res.carrier, str(x["origin"]).upper(), str(x["destination"]).upper(),
                                        float(x.get("minimum") or 0), dict(sorted(rates.items())),
                                        float(x.get("fuel") or 0), float(x.get("security") or 0),
                                        x.get("transit_days"), res.currency, res.valid_from, res.valid_to,
                                        surcharges_found=x.get("fuel") is not None))
        except (KeyError, TypeError, ValueError):
            continue
    return res


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------
def q2(x: float) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def price_lane(lane: TariffLane, weight_kg: float, volume_cbm: float = 0.0) -> dict:
    """Cheapest total over billed weights (chargeable or a higher break)."""
    cw = max(float(weight_kg), float(volume_cbm or 0) * 167.0)
    ks = sorted(lane.rates)
    s = lane.fuel + lane.security

    def total(billed: float) -> float:
        brk = max([k for k in ks if k <= billed] or [ks[0]])
        return max(lane.minimum, lane.rates[brk] * billed) + s * billed

    cands = [cw] + [float(k) for k in ks if k > cw]
    best_w = min(cands, key=lambda w: (round(total(w), 6), w))
    brk = max([k for k in ks if k <= best_w] or [ks[0]])
    return {"chargeable": cw, "billed": best_w, "total": q2(total(best_w)), "break": brk,
            "rate": lane.rates[brk], "freight": q2(max(lane.minimum, lane.rates[brk] * best_w)),
            "surcharges": q2(s * best_w)}


class TariffStore:
    """Tariff lanes in SQLite (table tariffs), newest sheet per carrier+lane wins."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS tariffs (
        carrier TEXT, origin TEXT, destination TEXT, transit_days INTEGER, minimum REAL,
        fuel REAL, security REAL, rates TEXT, currency TEXT, valid_from TEXT, valid_to TEXT,
        carrier_email TEXT, source TEXT, ingested_at TEXT,
        PRIMARY KEY (carrier, origin, destination)
    );
    """

    def __init__(self, conn, lock: threading.Lock | None = None):
        self.conn = conn
        self.lock = lock or threading.Lock()
        with self.lock, self.conn:
            self.conn.executescript(self.SCHEMA)

    def save(self, lanes: list[TariffLane]) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.lock, self.conn:
            for ln in lanes:
                self.conn.execute(
                    "INSERT OR REPLACE INTO tariffs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ln.carrier, ln.origin, ln.destination, ln.transit_days, ln.minimum, ln.fuel, ln.security,
                     json.dumps({str(k): v for k, v in ln.rates.items()}), ln.currency, ln.valid_from, ln.valid_to,
                     ln.carrier_email, ln.source, now))
        return len(lanes)

    def count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM tariffs").fetchone()[0]

    def lanes(self, origin: str, destination: str, today: date | None = None) -> list[TariffLane]:
        today_s = (today or datetime.now(timezone.utc).date()).isoformat()
        out = []
        for r in self.conn.execute("SELECT * FROM tariffs WHERE origin=? AND destination=?", (origin, destination)):
            r = dict(zip([c[0] for c in self.conn.execute("SELECT * FROM tariffs LIMIT 0").description], r)) \
                if not hasattr(r, "keys") else dict(r)
            if r["valid_from"] and r["valid_from"] > today_s:
                continue
            if r["valid_to"] and r["valid_to"] < today_s:
                continue
            out.append(TariffLane(r["carrier"], r["origin"], r["destination"], r["minimum"],
                                  {int(k): v for k, v in json.loads(r["rates"]).items()}, r["fuel"] or 0.0,
                                  r["security"] or 0.0, r["transit_days"], r["currency"] or "EUR",
                                  r["valid_from"], r["valid_to"], r["carrier_email"], r["source"] or ""))
        return out

    def seed_from_csv(self, path: str, emails: dict[str, str], valid_from: str | None = None,
                      valid_to: str | None = None) -> int:
        """Load data/*_tariffs_normalized_*.csv (long format) if those lanes are not stored yet."""
        f = Path(path)
        if not f.is_file():
            return 0
        lanes: dict[tuple, TariffLane] = {}
        with f.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                k = (row["carrier"], row["origin"], row["destination"])
                if k not in lanes:
                    lanes[k] = TariffLane(row["carrier"], row["origin"], row["destination"],
                                          float(row["minimum_eur"]), {}, float(row["fuel_eur_kg"] or 0),
                                          float(row["security_eur_kg"] or 0), int(float(row["transit_days"])),
                                          "EUR", valid_from, valid_to, emails.get(row["carrier"].lower()),
                                          f"seed:{f.name}", True)
                lanes[k].rates[int(float(row["break_kg"]))] = float(row["rate_eur_kg"])
        existing = {tuple(r) for r in self.conn.execute("SELECT carrier, origin, destination FROM tariffs")}
        new = [ln for k, ln in lanes.items() if k not in existing]
        return self.save(new)
