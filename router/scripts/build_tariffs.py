"""Build data/tariffs.json from the start-kit tariff files. Run once; the output is committed.

    .venv/bin/python scripts/build_tariffs.py

Each file is turned into plain text (txt as is, pdf via `pdftotext`, xlsx/docx via their zip
XML) and read by one LLM call into `list[TariffLane]` (ADR 12: human-written sources are read
by an LLM). Scanned pages (jpg) go in as images. Every lane is then sanity-checked in code.
"""

import asyncio
import base64
import json
import subprocess
import sys
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langfuse.openai import AsyncOpenAI  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.config import configure_langfuse_env, evaluation_settings  # noqa: E402
from app.reference import Reference  # noqa: E402
from app.schemas import TariffLane  # noqa: E402

AIRPORTS = {"CGN", "DUS", "FRA", "HAM", "MUC", "STR"}
SYSTEM_PROMPT = """You convert an airline's air export tariff into structured data.
Return one entry per lane (origin airport -> destination airport), for EVERY lane in the document.

Fields per lane
- origin, destination: 3-letter IATA codes.
- transit_days: integer ("D+4" = 4, "2 days" = 2).
- minimum_eur: the minimum charge.
- breaks_kg: the lower bound of every weight bracket, ascending, starting with 0
  ("-45" or "from 0 kg" is the bracket starting at 0; "+45" starts at 45).
- rates_per_kg: the rate of each bracket, same order and same length as breaks_kg.
- fuel_per_kg, security_per_kg: the surcharges of this lane. They may be listed in a
  separate section or on a separate page; match them to the lane.
- valid_from, valid_to: ISO dates. An Excel serial such as 46327 is 2026-11-01 (46356 = 2026-11-30).
- currency.
Leave airline and source_file empty.

Rules
- Copy every number exactly. Decimal commas become dots ("5,92" = 5.92). Drop float noise
  ("83.73999999999999" = 83.74). Never invent a lane or a number."""


class TariffFile(BaseModel):
    lanes: list[TariffLane]


def _xml_text(node: ElementTree.Element) -> str:
    return "".join(node.itertext())


def xlsx_text(path: Path) -> str:
    """Every sheet as tab-separated rows, with shared strings resolved."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        strings: list[str] = []  # files written with inline strings have no shared table
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = [_xml_text(si) for si in root.findall("m:si", ns)]
        rows: list[str] = []
        for name in sorted(n for n in archive.namelist() if n.startswith("xl/worksheets/sheet")):
            for row in ElementTree.fromstring(archive.read(name)).iter(f"{{{ns['m']}}}row"):
                cells = []
                for cell in row.findall("m:c", ns):
                    value = cell.find("m:v", ns)
                    text = _xml_text(cell) if value is None else value.text or ""
                    cells.append(strings[int(text)] if cell.get("t") == "s" else text)
                rows.append("\t".join(cells))
    return "\n".join(rows)


def docx_text(path: Path) -> str:
    """Paragraphs and table rows of a Word file, one per line."""
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as archive:
        body = ElementTree.fromstring(archive.read("word/document.xml")).find(f"{w}body")
    lines: list[str] = []
    for block in body:
        if block.tag == f"{w}tbl":
            lines += ["\t".join(_xml_text(c) for c in r.iter(f"{w}tc")) for r in block.iter(f"{w}tr")]
        else:
            lines.append(_xml_text(block))
    return "\n".join(lines)


def file_content(paths: list[Path]) -> list[dict]:
    """The user-message content for one airline: text, or images for scanned pages."""
    content: list[dict] = []
    for path in paths:
        suffix = path.suffix.lower()
        if suffix == ".jpg":
            data = base64.b64encode(path.read_bytes()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}})
            continue
        if suffix == ".pdf":
            text = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True
            ).stdout
        elif suffix == ".xlsx":
            text = xlsx_text(path)
        elif suffix == ".docx":
            text = docx_text(path)
        else:
            text = path.read_text(encoding="utf-8")
        content.append({"type": "text", "text": text})
    return content


def check(lane: TariffLane) -> None:
    """Fail loudly on anything a tariff lane cannot be."""
    where = f"{lane.airline} {lane.origin}-{lane.destination}"
    assert lane.origin in AIRPORTS, f"{where}: unknown origin"
    assert len(lane.destination) == 3 and lane.destination.isupper(), f"{where}: bad destination"
    assert len(lane.breaks_kg) == len(lane.rates_per_kg) >= 5, f"{where}: breaks/rates mismatch"
    assert lane.breaks_kg[0] == 0 and lane.breaks_kg == sorted(lane.breaks_kg), f"{where}: breaks"
    assert lane.rates_per_kg == sorted(lane.rates_per_kg, reverse=True), f"{where}: rates rise"
    assert 0.3 < lane.rates_per_kg[-1] and lane.rates_per_kg[0] < 15, f"{where}: rate range"
    assert 0 < lane.fuel_per_kg < 3 and 0 < lane.security_per_kg < 1, f"{where}: surcharges"
    assert 20 <= lane.minimum_eur <= 400 and 1 <= lane.transit_days <= 10, f"{where}: min/transit"
    assert lane.valid_from == date(2026, 11, 1) and lane.valid_to == date(2026, 11, 30), f"{where}: dates"
    assert lane.currency == "EUR", f"{where}: currency"


async def read_airline(client: AsyncOpenAI, model: str, airline: str, paths: list[Path]) -> list[TariffLane]:
    completion = await client.chat.completions.parse(
        name="build-tariffs",
        model=model,
        reasoning_effort="medium",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": file_content(paths)},
        ],
        response_format=TariffFile,
    )
    source = ", ".join(p.name for p in paths)
    lanes = [
        lane.model_copy(update={"airline": airline, "source_file": source})
        for lane in completion.choices[0].message.parsed.lanes
    ]
    for lane in lanes:
        check(lane)
    assert len({(l.origin, l.destination) for l in lanes}) == len(lanes), f"{airline}: duplicate lane"
    print(f"{airline}: {len(lanes)} lanes")
    return lanes


async def main() -> None:
    settings = evaluation_settings()
    configure_langfuse_env(settings)
    reference = Reference(settings.start_kit_dir)
    slugs = {c.email.split("@")[0]: c.name for c in reference.carriers if c.kind == "airline"}

    files: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(Path(settings.start_kit_dir).glob("*-tariff-*")):
        airline = next(name for slug, name in slugs.items() if path.name.startswith(slug))
        files[airline].append(path)

    client = AsyncOpenAI(api_key=settings.decision_api_key, base_url=settings.decision_api_base_url)
    results = await asyncio.gather(
        *(read_airline(client, settings.extract_model, a, p) for a, p in files.items())
    )
    lanes = sorted((l for r in results for l in r), key=lambda l: (l.airline, l.origin, l.destination))
    assert len(files) == 10, f"expected 10 tariff airlines, found {len(files)}"
    out = Path(settings.tariffs_path)
    out.write_text(json.dumps([l.model_dump(mode="json") for l in lanes], indent=1) + "\n")
    print(f"wrote {len(lanes)} lanes of {len(files)} airlines to {out}")


if __name__ == "__main__":
    asyncio.run(main())
