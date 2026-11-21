"""Apply the changes of one update mail to the reference store (ADR 50, docs/data-updates.md).

No LLM here: the LLM has read the mail (`DataChange`s) and flagged each change; this code
decides who may change what, checks every value, converts to EUR and writes everything in
ONE transaction. Rules:

- put: insert if missing, else overwrite only the fields given; lists (`serves`) only grow
- remove: only when the mail asks for it (the extractor returns `remove` only then)
- a change with a new start date adds a lane next to the old one; the newest wins on lookups
- held (not written): flagged suspicious, not allowed for the sender, or a failed check
"""

import calendar
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.reference import Carrier, Reference
from app.schemas import DataChange, TariffLane
from app.tariffs import Tariffs

# ponytail: the user's line. More than 4x (a rise of over 300 %) or under 1/4 of the old value
# is almost always a unit mistake (per 100 kg, per lb, cents); smaller changes are applied.
MAX_FACTOR = 4.0
_LANE_FIELDS = ("transit_days", "minimum_eur", "breaks_kg", "rates_per_kg", "fuel_per_kg", "security_per_kg", "valid_to")
_PRICE_FIELDS = ("minimum_eur", "fuel_per_kg", "security_per_kg")


class Held(Exception):
    """This change is not written; the message is the reason shown in the UI and the log."""


@dataclass
class Outcome:
    change_no: int
    target: str
    record_key: str
    field: str
    before: str | None
    after: str | None
    status: str  # applied | held
    flag: str
    reason: str = ""


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return " / ".join(_text(v) for v in value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _end_of_month(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


class _Apply:
    def __init__(self, ref: Reference, tariffs: Tariffs, message_id: int, sender: str | None,
                 today: date, fx_eur_per_usd: float | None) -> None:  # fmt: skip
        self.ref, self.tariffs, self.message_id, self.today = ref, tariffs, message_id, today
        self.fx = fx_eur_per_usd
        self.role = ref.role_for_email(sender)
        self.own = ref.carrier_for_email(sender)

    # --- who --------------------------------------------------------------------------

    def carrier(self, change: DataChange) -> Carrier | None:
        """The carrier this change is about; Held if the sender may not change it."""
        if self.role not in ("airline", "trucker", "ratedesk"):
            raise Held("only carriers and the rate desk may change our data")
        named = self._find(change.carrier) if change.carrier else None
        if self.role == "ratedesk":
            return named  # None: a carrier we do not know yet
        if change.target == "zone":
            raise Held("only the rate desk may change pickup zones")
        if named and named.name != self.own.name:
            raise Held(f"{self.own.name} may only change its own data, not {named.name}'s")
        return self.own  # a name variant ("Cobalt") from the carrier itself is still itself

    def _find(self, name: str) -> Carrier | None:
        wanted = name.strip().lower()
        carriers = self.ref.all_carriers
        exact = [c for c in carriers if c.name.lower() == wanted]
        loose = [c for c in carriers if wanted in c.name.lower() or c.name.lower() in wanted]
        return (exact or loose or [None])[0] if len(exact) == 1 or len(loose) == 1 else None

    # --- money ------------------------------------------------------------------------

    def eur_factor(self, change: DataChange) -> float:
        currency = (change.currency or "EUR").strip().upper()
        if currency in ("EUR", "€"):
            return 1.0
        if change.eur_per_unit:
            return change.eur_per_unit
        if currency in ("USD", "$") and self.fx:
            return self.fx
        raise Held(f"prices in {currency} without an exchange rate; we do not guess one")

    # --- lanes ------------------------------------------------------------------------

    def airports(self) -> set[str]:
        return {airport for _, airport in self.ref.zones.values()} | {l.origin for l in self.tariffs.all_lanes()}

    def lane_at(self, airline: str, origin: str, destination: str, day: date) -> TariffLane | None:
        valid = [
            l for l in self.tariffs.all_lanes(airline)
            if l.origin == origin and l.destination == destination and l.valid_from <= day <= l.valid_to
        ]  # fmt: skip
        return max(valid, key=lambda l: l.valid_from, default=None)

    def check_lane(self, lane: TariffLane) -> None:
        where = f"{lane.origin}-{lane.destination}"
        problems = [
            (lane.origin not in self.airports(), f"{where}: we have no pickups or lanes from {lane.origin}"),
            (not (len(lane.destination) == 3 and lane.destination.isalpha()), f"{where}: bad destination code"),
            (len(lane.breaks_kg) != len(lane.rates_per_kg) or not lane.breaks_kg,
             f"{where}: {len(lane.rates_per_kg)} rates for {len(lane.breaks_kg)} weight breaks"),
            (bool(lane.breaks_kg) and (lane.breaks_kg[0] != 0 or lane.breaks_kg != sorted(set(lane.breaks_kg))),
             f"{where}: weight breaks must start at 0 and rise"),
            (any(r <= 0 for r in lane.rates_per_kg), f"{where}: a rate is zero or negative"),
            (min(lane.minimum_eur, lane.fuel_per_kg, lane.security_per_kg) < 0, f"{where}: a negative charge"),
            (not 1 <= lane.transit_days <= 30, f"{where}: transit {lane.transit_days} days"),
            (lane.valid_from > lane.valid_to, f"{where}: valid from {lane.valid_from} after valid to {lane.valid_to}"),
        ]  # fmt: skip
        for failed, reason in problems:
            if failed:
                raise Held(reason)

    def check_jump(self, old: TariffLane, new: TariffLane) -> None:
        pairs = [(f, getattr(old, f), getattr(new, f)) for f in _PRICE_FIELDS]
        if len(old.rates_per_kg) == len(new.rates_per_kg):
            pairs += [(f"rate from {b:g} kg", o, n) for b, o, n in zip(new.breaks_kg, old.rates_per_kg, new.rates_per_kg)]
        for field, before, after in pairs:
            if before > 0 and after > 0 and not 1 / MAX_FACTOR <= after / before <= MAX_FACTOR:
                raise Held(f"suspicious: {field} {before:g} -> {after:g} (x{after / before:.1f}), likely a unit mistake")

    def write_lane(self, n: int, old: TariffLane | None, new: TariffLane, flag: str) -> list[Outcome]:
        """Check, then insert or overwrite `new`. `old` is the lane it replaces from its start."""
        self.check_lane(new)
        if old:
            self.check_jump(old, new)
        self.tariffs.put([new], self.message_id)
        key = f"{new.airline} {new.origin}-{new.destination} from {new.valid_from}"
        if old is None:
            summary = f"new lane, {_text(new.rates_per_kg)} EUR/kg, transit {new.transit_days} d"
            return [Outcome(n, "tariff_lane", key, "lane", None, summary, "applied", flag)]
        rows = [
            Outcome(n, "tariff_lane", key, f, _text(getattr(old, f)), _text(getattr(new, f)), "applied", flag)
            for f in _LANE_FIELDS
            if getattr(old, f) != getattr(new, f)
        ]
        return rows or [Outcome(n, "tariff_lane", key, "-", None, None, "applied", flag, "already up to date")]

    def put_lane(self, n: int, change: DataChange, carrier: Carrier | None, flag: str) -> list[Outcome]:
        airline = self._airline(carrier)
        if not (change.origin and change.destination):
            raise Held("a lane needs an origin and a destination airport")
        origin, destination = change.origin.strip().upper(), change.destination.strip().upper()
        start = change.valid_from or self.today
        factor = self.eur_factor(change)
        base = self.lane_at(airline, origin, destination, start)
        own_lanes = self.tariffs.all_lanes(airline)
        template = base or next(
            (l for l in own_lanes if change.copy_from_destination
             and l.destination == change.copy_from_destination.upper() and l.origin == origin), None,
        ) or next((l for l in own_lanes if l.origin == origin), None) or (own_lanes[-1] if own_lanes else None)  # fmt: skip
        given = {
            "transit_days": change.transit_days,
            "minimum_eur": None if change.minimum is None else round(change.minimum * factor, 2),
            "breaks_kg": change.breaks_kg,
            "rates_per_kg": None if change.rates_per_kg is None else [round(r * factor, 2) for r in change.rates_per_kg],
            "fuel_per_kg": None if change.fuel_per_kg is None else round(change.fuel_per_kg * factor, 2),
            "security_per_kg": None if change.security_per_kg is None else round(change.security_per_kg * factor, 2),
        }  # fmt: skip
        given = {k: v for k, v in given.items() if v is not None}
        if template is None:
            missing = [f for f in ("transit_days", "minimum_eur", "breaks_kg", "rates_per_kg", "fuel_per_kg", "security_per_kg") if f not in given]
            if missing:
                raise Held(f"new lane for an airline without lanes needs: {', '.join(missing)}")
            template = TariffLane(airline=airline, origin=origin, destination=destination, valid_from=start, valid_to=start, **given)
        if base and base.valid_from == start:
            valid_to = change.valid_to or base.valid_to
        else:
            valid_to = change.valid_to or (base.valid_to if base else _end_of_month(start))
        new = template.model_copy(update={
            **given, "airline": airline, "origin": origin, "destination": destination,
            "valid_from": start, "valid_to": valid_to, "currency": "EUR", "source_file": f"mail {self.message_id}",
        })  # fmt: skip
        return self.write_lane(n, base, new, flag)

    def remove_lane(self, n: int, change: DataChange, carrier: Carrier | None, flag: str) -> list[Outcome]:
        airline = self._airline(carrier)
        if not (change.origin and change.destination):
            raise Held("which lane to remove: origin and destination are missing")
        origin, destination = change.origin.strip().upper(), change.destination.strip().upper()
        stop = change.valid_from or self.today
        lanes = [l for l in self.tariffs.all_lanes(airline)
                 if l.origin == origin and l.destination == destination and l.valid_to >= stop]  # fmt: skip
        if not lanes:
            raise Held(f"{airline} has no lane {origin}-{destination} to remove")
        rows = []
        for lane in lanes:
            key = f"{airline} {origin}-{destination} from {lane.valid_from}"
            if lane.valid_from >= stop:
                self.tariffs.delete(lane)
                rows.append(Outcome(n, "tariff_lane", key, "lane", "exists", "removed", "applied", flag))
            else:
                self.tariffs.put([lane.model_copy(update={"valid_to": stop - timedelta(days=1)})], self.message_id)
                rows.append(Outcome(n, "tariff_lane", key, "valid_to", _text(lane.valid_to), _text(stop - timedelta(days=1)), "applied", flag))
        return rows

    def adjust(self, n: int, change: DataChange, carrier: Carrier | None, flag: str) -> list[Outcome]:
        """'+5 % from 1 Dec', 'fuel +0.10': the LLM gives the change, code does the maths."""
        airline = self._airline(carrier)
        if change.percent is None and change.amount is None:
            raise Held("a relative change needs a percent or an amount")
        start = change.valid_from or self.today
        amount = (change.amount or 0) * (self.eur_factor(change) if change.amount else 1)
        fields = change.adjust_fields or ["rates"]
        current = {(l.origin, l.destination) for l in self.tariffs.all_lanes(airline)}
        # The lane valid on the start day, else the latest one before it: "+5 % from 1 Dec"
        # builds on November's tariff even though it ends on 30 Nov.
        lanes = [
            lane for o, d in sorted(current)
            if (lane := self.lane_at(airline, o, d, start) or self._latest_before(airline, o, d, start))
            and (not change.origin or o == change.origin.upper())
            and (not change.destination or d == change.destination.upper())
        ]  # fmt: skip
        if not lanes:
            raise Held(f"{airline} has no tariff lanes valid on {start} to adjust")

        def moved(value: float) -> float:
            return round(value * (1 + change.percent / 100) if change.percent is not None else value + amount, 2)

        rows: list[Outcome] = []
        for lane in lanes:
            update = {"valid_from": start, "source_file": f"mail {self.message_id}",
                      "valid_to": change.valid_to or (lane.valid_to if lane.valid_to >= start else _end_of_month(start))}  # fmt: skip
            if "rates" in fields:
                update["rates_per_kg"] = [moved(r) for r in lane.rates_per_kg]
            for name, field in (("minimum", "minimum_eur"), ("fuel", "fuel_per_kg"), ("security", "security_per_kg")):
                if name in fields:
                    update[field] = moved(getattr(lane, field))
            rows += self.write_lane(n, lane, lane.model_copy(update=update), flag)
        return rows

    def _latest_before(self, airline: str, origin: str, destination: str, day: date) -> TariffLane | None:
        earlier = [l for l in self.tariffs.all_lanes(airline)
                   if l.origin == origin and l.destination == destination and l.valid_from <= day]  # fmt: skip
        return max(earlier, key=lambda l: l.valid_from, default=None)

    def _airline(self, carrier: Carrier | None) -> str:
        if carrier is None or carrier.kind != "airline":
            raise Held("tariff lanes need a known airline")
        return carrier.name

    # --- carriers ---------------------------------------------------------------------

    def _codes(self, kind: str, codes: list[str] | None) -> set[str]:
        clean = {c.strip().upper() for c in codes or [] if c.strip()}
        bad = [c for c in clean if not (c.isdigit() and len(c) == 2 if kind == "trucker" else c.isalpha() and len(c) == 3)]
        if bad:
            raise Held(f"not a {'2-digit postal zone' if kind == 'trucker' else '3-letter airport code'}: {', '.join(bad)}")
        return clean

    def _last_trucker(self, trucker: Carrier, zones: set[str]) -> None:
        orphaned = sorted(
            z for z in zones
            if not any(c.name != trucker.name and z in c.serves for c in self.ref.carriers if c.kind == "trucker")
        )  # fmt: skip
        if orphaned:
            raise Held(f"zone {', '.join(orphaned)} would have no trucker left")

    def put_carrier(self, n: int, change: DataChange, carrier: Carrier | None, flag: str) -> list[Outcome]:
        if carrier is None:
            if not (change.carrier and change.kind and change.email):
                raise Held("a new carrier needs a name, a kind (airline or trucker) and an email")
            serves = self._codes(change.kind, change.serves_add)
            self.ref.db.execute(
                "INSERT INTO carriers (name, kind, email, serves, source, source_message_id) VALUES (?, ?, ?, ?, 'mail', ?)",
                (change.carrier.strip(), change.kind, change.email.strip().lower(), " ".join(sorted(serves)), self.message_id),
            )  # fmt: skip
            summary = f"new {change.kind}, {change.email.strip().lower()}, serves {' '.join(sorted(serves)) or '-'}"
            return [Outcome(n, "carrier", change.carrier.strip(), "carrier", None, summary, "applied", flag)]
        if change.kind and change.kind != carrier.kind:
            raise Held(f"{carrier.name} is a {carrier.kind}, not a {change.kind}")
        row = self.ref.db.execute("SELECT * FROM carriers WHERE name = ?", (carrier.name,)).fetchone()
        stored = set(row["serves"].split())
        add, drop = self._codes(carrier.kind, change.serves_add), self._codes(carrier.kind, change.serves_remove)
        if carrier.kind == "trucker" and drop:
            self._last_trucker(carrier, drop & carrier.serves)
        serves = " ".join(sorted((stored | add) - drop))
        email, aliases = row["email"], set(row["aliases"].split())
        if change.email and change.email.strip().lower() != email:
            aliases.add(email)
            email = change.email.strip().lower()
            aliases.discard(email)
        new = {"email": email, "serves": serves, "aliases": " ".join(sorted(aliases)), "active": 1}
        rows = [
            Outcome(n, "carrier", carrier.name, field, _text(row[field]) or "-", _text(value) or "-", "applied", flag)
            for field, value in new.items()
            if field != "aliases" and str(row[field]) != str(value)
        ]
        self.ref.db.execute(
            "UPDATE carriers SET email = ?, serves = ?, aliases = ?, active = 1, source = 'mail', source_message_id = ? WHERE name = ?",
            (email, serves, new["aliases"], self.message_id, carrier.name),
        )
        return rows or [Outcome(n, "carrier", carrier.name, "-", None, None, "applied", flag, "already up to date")]

    def remove_carrier(self, n: int, change: DataChange, carrier: Carrier | None, flag: str) -> list[Outcome]:
        if carrier is None:
            raise Held(f"unknown carrier {change.carrier!r}")
        if change.serves_remove:  # "we no longer serve zone 45": only that, not the carrier
            return self.put_carrier(n, change.model_copy(update={"serves_add": None}), carrier, flag)
        if carrier.kind == "trucker":
            self._last_trucker(carrier, set(carrier.serves))
        self.ref.db.execute("UPDATE carriers SET active = 0, source = 'mail', source_message_id = ? WHERE name = ?",
                            (self.message_id, carrier.name))  # fmt: skip
        return [Outcome(n, "carrier", carrier.name, "active", "yes", "no (stopped)", "applied", flag)]

    # --- zones ------------------------------------------------------------------------

    def put_zone(self, n: int, change: DataChange, carrier: Carrier | None, flag: str) -> list[Outcome]:
        zone = (change.zone or "").strip()[:2]
        if not (zone.isdigit() and len(zone) == 2):
            raise Held(f"not a 2-digit postal zone: {change.zone!r}")
        old = self.ref.zones.get(zone)
        airport = (change.airport or (old[1] if old else "")).strip().upper()
        if airport not in self.airports():
            raise Held(f"zone {zone} -> {airport or '?'}: we have no lanes from that airport")
        area = change.area or (old[0] if old else "")
        self.ref.db.execute(
            "INSERT OR REPLACE INTO pickup_zones (zone, area, airport, source, source_message_id) VALUES (?, ?, ?, 'mail', ?)",
            (zone, area, airport, self.message_id),
        )
        if old is None:
            return [Outcome(n, "zone", f"zone {zone}", "zone", None, f"{area} -> {airport}".strip(), "applied", flag)]
        rows = [Outcome(n, "zone", f"zone {zone}", f, b, a, "applied", flag)
                for f, b, a in (("airport", old[1], airport), ("area", old[0], area)) if b != a]  # fmt: skip
        return rows or [Outcome(n, "zone", f"zone {zone}", "-", None, None, "applied", flag, "already up to date")]

    def remove_zone(self, n: int, change: DataChange, carrier: Carrier | None, flag: str) -> list[Outcome]:
        # ponytail: never seen; ask for the zone's new airport instead of dropping pickups there
        raise Held("removing a pickup zone is not supported; tell us which airport serves it instead")


_ROUTES = {
    ("put", "tariff_lane"): _Apply.put_lane,
    ("remove", "tariff_lane"): _Apply.remove_lane,
    ("adjust", "tariff_lane"): _Apply.adjust,
    ("adjust", "carrier"): _Apply.adjust,
    ("put", "carrier"): _Apply.put_carrier,
    ("remove", "carrier"): _Apply.remove_carrier,
    ("put", "zone"): _Apply.put_zone,
    ("remove", "zone"): _Apply.remove_zone,
}


def _key(change: DataChange) -> str:
    if change.target == "zone":
        return f"zone {change.zone or '?'}"
    if change.target == "tariff_lane" and change.origin:
        return f"{change.carrier or '?'} {change.origin}-{change.destination or '?'}"
    return change.carrier or "?"


def apply_update(
    ref: Reference,
    tariffs: Tariffs,
    *,
    message_id: int,
    sender: str | None,
    subject: str,
    changes: list[DataChange],
    flags: dict[int, tuple[str, str]],
    today: date,
    fx_eur_per_usd: float | None = None,
) -> list[Outcome]:
    """Apply what passes, hold the rest, log both. One transaction for the whole mail."""
    run = _Apply(ref, tariffs, message_id, sender, today, fx_eur_per_usd)
    outcomes: list[Outcome] = []
    now = datetime.now(UTC).isoformat()
    with ref.db:  # ref.db is tariffs.db: one connection per database file
        for n, change in enumerate(changes, start=1):
            flag, reason = flags.get(n, ("ok", ""))
            try:
                if flag == "suspicious":
                    raise Held(f"suspicious: {reason}" if reason else "flagged suspicious")
                outcomes += _ROUTES[(change.action, change.target)](run, n, change, run.carrier(change), flag)
            except Held as held:
                why = str(held)
                outcomes.append(Outcome(n, change.target, _key(change), "-", None, None, "held",
                                        "suspicious" if why.startswith("suspicious") else flag, why))  # fmt: skip
            else:
                if flag == "unclear" and reason:
                    for o in outcomes:
                        if o.change_no == n and not o.reason:
                            o.reason = reason
        applied = {o.change_no for o in outcomes if o.status == "applied"}
        status = (
            "nothing_found" if not changes else "needs_review" if not applied
            else "applied" if len(applied) == len(changes) else "partly_applied"
        )  # fmt: skip
        ref.db.execute(
            "INSERT OR REPLACE INTO data_updates (message_id, sender, subject, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (message_id, sender, subject, status, now),
        )
        ref.db.executemany(
            "INSERT INTO data_changes (message_id, change_no, target, record_key, field, before, after, status, flag, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(message_id, o.change_no, o.target, o.record_key, o.field, o.before, o.after, o.status, o.flag, o.reason, now)
             for o in outcomes],
        )  # fmt: skip
    return outcomes


def already_applied(ref: Reference, message_id: int) -> bool:
    return ref.db.execute("SELECT 1 FROM data_updates WHERE message_id = ?", (message_id,)).fetchone() is not None


def set_confirmation(ref: Reference, message_id: int, confirmation_id: int) -> None:
    with ref.db:
        ref.db.execute("UPDATE data_updates SET confirmation_message_id = ? WHERE message_id = ?", (confirmation_id, message_id))


def current_rows(ref: Reference, tariffs: Tariffs) -> dict[str, list[dict]]:
    """Everything in the store, for GET /reference."""
    return {
        "carriers": [c._asdict() | {"serves": sorted(c.serves), "aliases": sorted(c.aliases)} for c in ref.all_carriers],
        "zones": [{"zone": z, "area": a, "airport": p} for z, (a, p) in sorted(ref.zones.items())],
        "tariffs": [json.loads(l.model_dump_json()) for l in tariffs.all_lanes()],
    }
