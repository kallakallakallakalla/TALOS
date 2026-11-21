"""Start-kit reference data: who a sender is, which airport and truckers serve a postal zone.

Plain CSV parsing only (ADR 12 allows it for CSV data).
"""

import csv
from functools import cached_property
from pathlib import Path
from typing import NamedTuple

from app.schemas import SenderRole

_DESK_ROLES: dict[str, SenderRole] = {"ratedesk": "ratedesk", "tracking": "tracking"}


class Carrier(NamedTuple):
    name: str
    kind: str  # "airline" | "trucker"
    email: str
    serves: frozenset[str]  # airline: destination IATA codes; trucker: 2-digit postal zones


def zone_of(postal_code: str) -> str:
    """A pickup zone is the first two digits of the German postal code (ADR 05)."""
    return postal_code.strip()[:2]


class Reference:
    """Cached lookups over `carrier-list.csv` and `pickup_zones.csv`."""

    def __init__(self, start_kit_dir: str) -> None:
        self._dir = Path(start_kit_dir)

    def _rows(self, filename: str) -> list[dict[str, str]]:
        with (self._dir / filename).open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    @cached_property
    def carriers(self) -> list[Carrier]:
        return [
            Carrier(r["name"], r["kind"], r["email"].lower(), frozenset(r["serves"].split()))
            for r in self._rows("carrier-list.csv")
        ]

    @cached_property
    def _by_email(self) -> dict[str, Carrier]:
        return {c.email: c for c in self.carriers}

    @cached_property
    def zones(self) -> list[dict[str, str]]:
        """Rows of `pickup_zones.csv`: zone, area, airport."""
        return self._rows("pickup_zones.csv")

    @cached_property
    def _zone_airports(self) -> dict[str, str]:
        return {r["zone"]: r["airport"] for r in self.zones}

    def airport_for_zone(self, zone: str) -> str | None:
        """Departure airport for a pickup zone, or None if we do not serve the zone."""
        return self._zone_airports.get(zone)

    def truckers_for_zone(self, zone: str) -> list[Carrier]:
        """Only the truckers whose `serves` lists the zone are asked (ADR 05)."""
        return [c for c in self.carriers if c.kind == "trucker" and zone in c.serves]

    def carrier_for_email(self, email: str | None) -> Carrier | None:
        return self._by_email.get((email or "").lower())

    def role_for_email(self, email: str | None) -> SenderRole:
        """Carriers and the two desks are known addresses; everyone else is a customer."""
        carrier = self.carrier_for_email(email)
        if carrier:
            return "airline" if carrier.kind == "airline" else "trucker"
        return _DESK_ROLES.get((email or "").split("@")[0].lower(), "customer")

    def airline_email(self, airline_name: str) -> str | None:
        """Booking address of an airline, matched on its exact carrier-list name."""
        wanted = airline_name.strip().lower()
        return next(
            (c.email for c in self.carriers if c.kind == "airline" and c.name.lower() == wanted),
            None,
        )
