"""Who a sender is, which airport and truckers serve a postal zone (ADR 05, ADR 50).

The data lives in the reference store (`app/refdb.py`) and is read on every lookup, so an
update mail counts from the next mail on. `carrier-list.csv` and `pickup_zones.csv` only seed
the tables while they are empty. Plain CSV parsing only (ADR 12 allows it for CSV data).
"""

import csv
from pathlib import Path
from typing import NamedTuple

from app import refdb
from app.schemas import SenderRole

_DESK_ROLES: dict[str, SenderRole] = {"ratedesk": "ratedesk", "tracking": "tracking"}


class Carrier(NamedTuple):
    name: str
    kind: str  # "airline" | "trucker"
    email: str
    serves: frozenset[str]  # airline: destination IATA codes; trucker: 2-digit postal zones
    active: bool = True
    aliases: frozenset[str] = frozenset()


def zone_of(postal_code: str) -> str:
    """A pickup zone is the first two digits of the German postal code (ADR 05)."""
    return postal_code.strip()[:2]


class Reference:
    """Lookups over the `carriers` and `pickup_zones` tables."""

    def __init__(self, start_kit_dir: str, db_path: str | None = None) -> None:
        self._dir = Path(start_kit_dir)
        self.db = refdb.connect(db_path)
        self._seed()

    def _rows(self, filename: str) -> list[dict[str, str]]:
        with (self._dir / filename).open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def _seed(self) -> None:
        with self.db:
            if refdb.is_empty(self.db, "carriers"):
                self.db.executemany(
                    "INSERT INTO carriers (name, kind, email, serves) VALUES (?, ?, ?, ?)",
                    [(r["name"], r["kind"], r["email"].lower(), r["serves"]) for r in self._rows("carrier-list.csv")],
                )
            if refdb.is_empty(self.db, "pickup_zones"):
                self.db.executemany(
                    "INSERT INTO pickup_zones (zone, area, airport) VALUES (?, ?, ?)",
                    [(r["zone"], r["area"], r["airport"]) for r in self._rows("pickup_zones.csv")],
                )

    @property
    def all_carriers(self) -> list[Carrier]:
        """Every carrier, inactive ones included. An airline also serves the destinations of
        its tariff lanes, so a new lane needs no second write (ADR 50, no duplicated facts)."""
        lane_dests: dict[str, set[str]] = {}
        for row in self.db.execute("SELECT DISTINCT airline, destination FROM tariff_lanes"):
            lane_dests.setdefault(row["airline"], set()).add(row["destination"])
        return [
            Carrier(
                r["name"], r["kind"], r["email"],
                frozenset(r["serves"].split()) | lane_dests.get(r["name"], set()),
                bool(r["active"]), frozenset(r["aliases"].split()),
            )
            for r in self.db.execute("SELECT * FROM carriers ORDER BY rowid")
        ]  # fmt: skip

    @property
    def carriers(self) -> list[Carrier]:
        """The carriers new requests may go to."""
        return [c for c in self.all_carriers if c.active]

    @property
    def zones(self) -> dict[str, tuple[str, str]]:
        """`zone -> (area, airport)`."""
        return {r["zone"]: (r["area"], r["airport"]) for r in self.db.execute("SELECT * FROM pickup_zones")}

    def airport_for_zone(self, zone: str) -> str | None:
        """Departure airport for a pickup zone, or None if we do not serve the zone."""
        row = self.db.execute("SELECT airport FROM pickup_zones WHERE zone = ?", (zone,)).fetchone()
        return row["airport"] if row else None

    def truckers_for_zone(self, zone: str) -> list[Carrier]:
        """Only the truckers whose `serves` lists the zone are asked (ADR 05)."""
        return [c for c in self.carriers if c.kind == "trucker" and zone in c.serves]

    def carrier_for_email(self, email: str | None) -> Carrier | None:
        """Also inactive carriers and earlier addresses: late replies and bookings still match."""
        wanted = (email or "").lower()
        return next((c for c in self.all_carriers if wanted == c.email or wanted in c.aliases), None)

    def carrier_named(self, name: str | None) -> Carrier | None:
        wanted = (name or "").strip().lower()
        return next((c for c in self.all_carriers if c.name.lower() == wanted), None)

    def role_for_email(self, email: str | None) -> SenderRole:
        """Carriers and the two desks are known addresses; everyone else is a customer."""
        carrier = self.carrier_for_email(email)
        if carrier:
            return "airline" if carrier.kind == "airline" else "trucker"
        return _DESK_ROLES.get((email or "").split("@")[0].lower(), "customer")

    def airline_email(self, airline_name: str) -> str | None:
        """Booking address of an airline, matched on its exact name (inactive ones too, so
        a quote sent before a carrier stopped can still be booked)."""
        carrier = self.carrier_named(airline_name)
        return carrier.email if carrier and carrier.kind == "airline" else None
