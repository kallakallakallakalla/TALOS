"""The local tariff DB: the ten airlines that are priced from tariff files, not the portal."""

import json
from datetime import date
from pathlib import Path

from app.schemas import TariffLane


class Tariffs:
    """Lanes from `data/tariffs.json` (built once by `scripts/build_tariffs.py`)."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lanes: list[TariffLane] = []
        if self._path.exists():
            self._lanes = [TariffLane.model_validate(row) for row in json.loads(self._path.read_text())]

    @property
    def airlines(self) -> set[str]:
        return {lane.airline for lane in self._lanes}

    def lanes(self, origin: str, destination: str, today: date) -> list[TariffLane]:
        """Lanes for a route that are valid on `today` (ADR 06)."""
        return [
            lane
            for lane in self._lanes
            if lane.origin == origin
            and lane.destination == destination
            and lane.valid_from <= today <= lane.valid_to
        ]

    def replace_airline(self, airline: str, lanes: list[TariffLane]) -> None:
        """A newer tariff replaces ALL lanes of that airline (ADR 06). Hook for tariff mails."""
        self._lanes = [lane for lane in self._lanes if lane.airline != airline] + lanes
        self._path.write_text(
            json.dumps([lane.model_dump(mode="json") for lane in self._lanes], indent=1) + "\n"
        )
