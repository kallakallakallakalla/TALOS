"""Lane lookup (/ports), weight breaks (/brackets), rates (/rates) and best-price pick."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .api import ApiError, FiveUClient
from .extract import Shipment

log = logging.getLogger(__name__)


def dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True)
class Airline:
    code: str
    name: str

    @property
    def label(self) -> str:
        if not self.code:
            return self.name
        return f"{self.name} ({self.code})" if self.name and self.name != self.code else self.code


class LaneMap:
    """(origin, destination) -> airlines, refreshed from GET /v1/portal/ports."""

    def __init__(self, client: FiveUClient, refresh_s: float = 300):
        self.client = client
        self.refresh_s = refresh_s
        self.lanes: dict[tuple[str, str], list[Airline]] = {}
        self.codes: set[str] = set()
        self._loaded_at = 0.0
        self._last_forced = 0.0
        self._lock = threading.Lock()

    def refresh(self) -> None:
        data = self.client.ports()
        raw = data.get("lanes", []) if isinstance(data, dict) else data or []
        lanes: dict[tuple[str, str], list[Airline]] = {}
        for lane in raw:
            o = str(lane.get("origin", "")).upper().strip()
            d = str(lane.get("destination", "")).upper().strip()
            if not o or not d:
                continue
            airlines = []
            for a in lane.get("airlines") or []:
                if isinstance(a, dict):
                    airlines.append(Airline(str(a.get("code") or a.get("name")), str(a.get("name") or a.get("code"))))
                elif isinstance(a, str):
                    airlines.append(Airline(a, a))
            lanes[(o, d)] = airlines
        with self._lock:
            self.lanes = lanes
            self.codes = {c for pair in lanes for c in pair}
            self._loaded_at = time.monotonic()
        log.info("ports: %d lanes, %d airports", len(lanes), len(self.codes))

    def ensure_fresh(self) -> None:
        if time.monotonic() - self._loaded_at > self.refresh_s:
            try:
                self.refresh()
            except ApiError as exc:
                log.warning("ports refresh failed: %s (keeping old map)", exc)

    def airlines(self, origin: str, destination: str) -> list[Airline]:
        self.ensure_fresh()
        found = self.lanes.get((origin, destination))
        if not found and time.monotonic() - self._last_forced > 30:
            self._last_forced = time.monotonic()  # lane may be new: force one refresh
            try:
                self.refresh()
            except ApiError as exc:
                log.warning("forced ports refresh failed: %s", exc)
            found = self.lanes.get((origin, destination))
        return list(found or [])


class BracketCache:
    def __init__(self, client: FiveUClient, ttl_s: float = 300):
        self.client = client
        self.ttl_s = ttl_s
        self._data: dict[str, tuple[float, list[int]]] = {}
        self._lock = threading.Lock()

    def get(self, airline_param: str, deadline: float | None) -> list[int] | None:
        with self._lock:
            hit = self._data.get(airline_param)
        if hit and time.monotonic() - hit[0] < self.ttl_s:
            return hit[1]
        try:
            data = self.client.brackets(airline_param, deadline=deadline)
        except ApiError as exc:
            log.info("brackets %s failed: %s", airline_param, exc)
            return None
        breaks = sorted({int(float(b)) for b in (data or {}).get("breaks_kg", []) if dec(b) is not None})
        with self._lock:
            self._data[airline_param] = (time.monotonic(), breaks)
        return breaks


@dataclass
class AirlineResult:
    airline: Airline
    rate: dict | None = None  # chosen rate (cheaper of actual weight vs next break)
    base_rate: dict | None = None
    break_rate: dict | None = None
    next_break: int | None = None
    break_applied: bool = False
    error: str | None = None

    @property
    def total(self) -> Decimal | None:
        return dec(self.rate.get("total")) if self.rate else None

    @property
    def currency(self) -> str:
        return str(self.rate.get("currency", "")).upper() if self.rate else ""


@dataclass
class LotQuote:
    shipment: Shipment
    results: list[AirlineResult] = field(default_factory=list)
    best: AirlineResult | None = None
    flags: list[str] = field(default_factory=list)
    pickup: dict | None = None  # {zip, city, airport, price_eur, trucker}


def total_in_eur(r: AirlineResult) -> Decimal | None:
    if r.total is None:
        return None
    if r.currency == "EUR":
        return r.total
    fx = dec(r.rate.get("fx_eur_per_usd")) if r.rate else None
    if r.currency == "USD" and fx:
        return r.total * fx
    return None


def grand_total(lot: "LotQuote") -> tuple[Decimal | None, str, Decimal | None]:
    """(total incl. pickup, currency, pickup in that currency)."""
    if not lot.best or lot.best.total is None:
        return None, "", None
    total, cur = lot.best.total, lot.best.currency
    pe = dec((lot.pickup or {}).get("price_eur"))
    if pe is None:
        return total, cur, None
    if cur == "EUR":
        conv = pe
    else:
        fx = dec(lot.best.rate.get("fx_eur_per_usd")) if cur == "USD" else None
        conv = (pe / fx).quantize(Decimal("0.01")) if fx else pe
    return (total + conv).quantize(Decimal("0.01")), cur, conv


class Pricer:
    def __init__(self, client: FiveUClient, lanes: LaneMap, brackets: BracketCache, workers: int = 8,
                 tariffs=None):
        self.client = client
        self.lanes = lanes
        self.brackets = brackets
        self.workers = workers
        self.tariffs = tariffs  # TariffStore: airlines from rate sheets (booked by e-mail)
        self.airline_key = "code"  # switches to "name" if the portal rejects codes

    def _param(self, a: Airline) -> str:
        return a.name if self.airline_key == "name" else a.code

    def _rate(self, a: Airline, s: Shipment, weight: float, deadline: float) -> dict:
        try:
            return self.client.rate(
                s.origin, s.destination, self._param(a), weight, s.volume_cbm or 0, deadline=deadline
            )
        except ApiError as exc:
            if exc.status in (404, 422) and self.airline_key == "code" and a.name != a.code:
                rate = self.client.rate(
                    s.origin, s.destination, a.name, weight, s.volume_cbm or 0, deadline=deadline
                )
                log.warning("portal accepts airline names, not codes: switching")
                self.airline_key = "name"
                return rate
            raise

    def _price_airline(self, a: Airline, s: Shipment, deadline: float) -> AirlineResult:
        res = AirlineResult(a)
        try:
            base = self._rate(a, s, s.weight_kg, deadline)
        except ApiError as exc:
            res.error = f"{exc.status}: {str(exc.detail)[:80]}"
            return res
        res.rate = res.base_rate = base

        # break-point check: price at the next weight break, keep the cheaper total
        chargeable = dec(base.get("chargeable_weight_kg")) or Decimal(str(s.weight_kg))
        breaks = self.brackets.get(self._param(a), deadline)
        nxt = next((b for b in breaks or [] if Decimal(b) > chargeable), None)
        if nxt is None:
            return res
        res.next_break = nxt
        try:
            br = self._rate(a, s, float(nxt), deadline)
        except ApiError as exc:
            log.info("break rate %s @%skg failed: %s", a.code, nxt, exc)
            return res
        res.break_rate = br
        bt, tt = dec(br.get("total")), dec(base.get("total"))
        if bt is not None and tt is not None and bt < tt:
            res.rate = br
            res.break_applied = True
        return res

    def _price_tariff(self, t, s: Shipment) -> AirlineResult:
        from .tariffs import price_lane

        p = price_lane(t, float(s.weight_kg), float(s.volume_cbm or 0))
        slug = re.sub(r"[^A-Z]", "", t.carrier.upper())[:8] or "TARIFF"
        rate = {
            "rate_id": f"TAR-{slug}-{s.origin}{s.destination}-{int(time.time() * 1000) % 10**9}",
            "airline": t.carrier, "airline_code": "", "origin": s.origin, "destination": s.destination,
            "weight_kg": f"{p['billed']:g}", "chargeable_weight_kg": f"{p['chargeable']:.1f}",
            "total": str(p["total"]), "currency": t.currency or "EUR", "transit_days": t.transit_days,
            "valid_until": f"{t.valid_to}T23:59:59Z" if t.valid_to else None, "source": "tariff",
            "carrier_email": t.carrier_email, "tariff_source": t.source,
            "breakdown": {"break_kg": p["break"], "rate": p["rate"], "freight": str(p["freight"]),
                          "surcharges": str(p["surcharges"]), "minimum": t.minimum},
        }
        res = AirlineResult(Airline("", t.carrier), rate=rate, base_rate=rate)
        res.break_applied = p["billed"] > p["chargeable"]
        return res

    def quote(self, s: Shipment, deadline: float) -> LotQuote:  # noqa: C901
        lot = LotQuote(s)
        if not s.origin or not s.destination:
            lot.flags.append("origin/destination missing")
            return lot
        if not s.weight_kg or s.weight_kg <= 0:
            lot.flags.append("weight missing")
            return lot
        airlines = self.lanes.airlines(s.origin, s.destination)
        tariff_lanes = []
        if self.tariffs is not None:
            portal = {a.name.lower() for a in airlines}
            tariff_lanes = [t for t in self.tariffs.lanes(s.origin, s.destination) if t.carrier.lower() not in portal]
        if not airlines and not tariff_lanes:
            lot.flags.append(f"lane {s.origin}-{s.destination} not served")
            return lot

        if airlines:
            with ThreadPoolExecutor(max_workers=max(1, min(self.workers, len(airlines)))) as ex:
                lot.results = list(ex.map(lambda a: self._price_airline(a, s, deadline), airlines))
        for t in tariff_lanes:
            try:
                lot.results.append(self._price_tariff(t, s))
            except Exception as exc:  # noqa: BLE001
                log.warning("tariff price %s %s-%s failed: %s", t.carrier, s.origin, s.destination, exc)

        priced = [r for r in lot.results if r.total is not None]
        if not priced:
            lot.flags.append("no rate returned")
            return lot

        if s.priority == "fastest":
            def transit(r: AirlineResult) -> int:
                try:
                    return int(r.rate.get("transit_days"))
                except (TypeError, ValueError):
                    return 999
            lot.best = min(priced, key=lambda r: (transit(r), total_in_eur(r) or r.total))
            lot.flags.append("fastest requested: shortest transit, then cheapest")
        elif len({r.currency for r in priced}) == 1:
            lot.best = min(priced, key=lambda r: r.total)
        else:
            comparable = [(total_in_eur(r), r) for r in priced]
            comparable = [(v, r) for v, r in comparable if v is not None]
            if comparable:
                lot.best = min(comparable, key=lambda x: x[0])[1]
                lot.flags.append("mixed currencies: compared in EUR via fx_eur_per_usd")
            else:
                lot.best = min(priced, key=lambda r: r.total)
                lot.flags.append("mixed currencies without fx: compared raw totals")
        return lot
