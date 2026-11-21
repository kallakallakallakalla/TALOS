"""The local tariff DB: lanes of the airlines priced from tariffs, not the portal (ADR 06, 50).

Lanes live in the `tariff_lanes` table and are read on every lookup. `data/tariffs.json`
(built once by `scripts/build_tariffs.py`) only seeds the table while it is empty.
"""

import json
from datetime import date
from pathlib import Path

from app import refdb
from app.schemas import TariffLane


class Tariffs:
    def __init__(self, path: str, db_path: str | None = None) -> None:
        self.db = refdb.connect(db_path)
        seed = Path(path)
        if seed.exists() and refdb.is_empty(self.db, "tariff_lanes"):
            with self.db:
                self.put([TariffLane.model_validate(row) for row in json.loads(seed.read_text())])

    @property
    def airlines(self) -> set[str]:
        return {r["airline"] for r in self.db.execute("SELECT DISTINCT airline FROM tariff_lanes")}

    def all_lanes(self, airline: str | None = None) -> list[TariffLane]:
        rows = self.db.execute(
            "SELECT lane_json FROM tariff_lanes WHERE ? IS NULL OR airline = ? "
            "ORDER BY airline, origin, destination, valid_from",
            (airline, airline),
        )
        return [TariffLane.model_validate_json(r["lane_json"]) for r in rows]

    def lanes(self, origin: str, destination: str, today: date) -> list[TariffLane]:
        """Lanes for a route that are valid on `today` (ADR 06), one per airline: when two
        overlap, the latest `valid_from` wins, then the later mail (ADR 50)."""
        rows = self.db.execute(
            """
            SELECT lane_json FROM tariff_lanes
            WHERE origin = ? AND destination = ? AND valid_from <= ? AND valid_to >= ?
            ORDER BY airline, valid_from DESC, COALESCE(source_message_id, 0) DESC
            """,
            (origin, destination, today.isoformat(), today.isoformat()),
        )
        best: dict[str, TariffLane] = {}
        for row in rows:
            lane = TariffLane.model_validate_json(row["lane_json"])
            best.setdefault(lane.airline, lane)
        return list(best.values())

    def put(self, lanes: list[TariffLane], message_id: int | None = None) -> None:
        """Insert, or overwrite the lane with the same airline, route and `valid_from`.
        The caller owns the transaction (`with tariffs.db:`)."""
        self.db.executemany(
            "INSERT OR REPLACE INTO tariff_lanes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    l.airline, l.origin, l.destination, l.valid_from.isoformat(), l.valid_to.isoformat(),
                    l.model_dump_json(), "mail" if message_id else "start_kit", message_id,
                )
                for l in lanes
            ],
        )  # fmt: skip

    def delete(self, lane: TariffLane) -> None:
        self.db.execute(
            "DELETE FROM tariff_lanes WHERE airline = ? AND origin = ? AND destination = ? AND valid_from = ?",
            (lane.airline, lane.origin, lane.destination, lane.valid_from.isoformat()),
        )
