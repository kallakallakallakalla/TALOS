#!/usr/bin/env python3
"""Every air price we can get for one shipment, cheapest first.

  tools/quote_air.py ORIGIN DEST GROSS_KG VOLUME_CBM --date <ready date YYYY-MM-DD>

Sources: the portal (live rate per airline on the lane) and our local tariffs
(data/tariff_lanes.csv). Per airline the portal wins over the local tariff. Anti-weight-break
is applied on both: billing the next break weight at its lower rate can be cheaper.
Output: JSON {chargeable_kg, options[], not_priced[]}. A portal option carries the rate_id to
book; a tariff option carries the airline's email (those are booked by mail).
Portal limit: 10 rate reads per minute, shared with the live system. Call this once per shipment.
A portal airline under not_priced with status 503: read its rate from the portal web page (see the playbook).
"""

import csv
import json
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parent))
from api import ROOT, call  # noqa: E402

KG_PER_CBM = 167
NEXT_BREAK_WITHIN = 1.5  # a break further away cannot be cheaper (rates drop < 35 % per break)


def rows(name: str) -> list[dict]:
    with open(ROOT / "data" / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def tariff_total(lane: dict, chargeable: float) -> tuple[float, float]:
    """Cheapest way to bill `chargeable` kg on a tariff lane: (total_eur, billed_kg).
    The minimum applies to the freight only; fuel and security are per billed kg on top."""
    breaks = [float(x) for x in lane["breaks_kg"].split()]
    rates = [float(x) for x in lane["rates_per_kg"].split()]
    own = max(i for i, start in enumerate(breaks) if start <= chargeable)
    candidates = []
    for i in range(own, len(breaks)):
        billed = max(chargeable, breaks[i])
        freight = max(float(lane["minimum_eur"]), billed * rates[i])
        candidates.append((round(freight + billed * (float(lane["fuel_per_kg"]) + float(lane["security_per_kg"])), 2), billed))
    return min(candidates)


def portal_rate(origin: str, dest: str, code: str, kg: float, cbm: float) -> tuple[int, dict]:
    query = urlencode({"origin": origin, "destination": dest, "airline": code, "weight_kg": kg, "volume_cbm": cbm})
    return call("GET", f"/v1/portal/rates?{query}")


def eur(rate: dict) -> float:
    total = float(rate["total"])
    if rate.get("currency", "EUR") != "EUR":  # the portal then gives fx_eur_per_usd
        total *= float(rate["fx_eur_per_usd"])
    return round(total, 2)


def quote(origin: str, dest: str, kg: float, cbm: float, on: date) -> dict:
    chargeable = round(max(kg, cbm * KG_PER_CBM), 2)
    emails = {c["name"]: c["email"] for c in rows("carriers.csv") if c["kind"] == "airline" and c["active"] != "0"}
    options, not_priced = [], []

    status, ports = call("GET", "/v1/portal/ports")
    lane = next((l for l in ports.get("lanes", []) if l["origin"] == origin and l["destination"] == dest), None) if status == 200 else None
    if status != 200:
        not_priced.append({"source": "portal", "status": status, "message": ports})
    deadline = time.monotonic() + 60  # the portal limit (10 reads/min) is shared; never hang on it
    for airline in lane["airlines"] if lane else []:
        if time.monotonic() > deadline:
            not_priced.append({"airline": airline["name"], "source": "portal", "status": 429,
                               "message": "skipped: portal rate limit, out of time", "email": emails.get(airline["name"])})
            continue
        status, rate = portal_rate(origin, dest, airline["code"], kg, cbm)
        if status != 200:
            not_priced.append({"airline": airline["name"], "source": "portal", "status": status, "message": rate,
                               "email": emails.get(airline["name"])})
            continue
        best, billed = rate, chargeable
        _, brackets = call("GET", f"/v1/portal/brackets?airline={airline['code']}")
        higher = [b for b in (brackets.get("breaks_kg", []) if isinstance(brackets, dict) else []) if b > chargeable]
        if higher and min(higher) <= chargeable * NEXT_BREAK_WITHIN and time.monotonic() < deadline:
            status, at_break = portal_rate(origin, dest, airline["code"], min(higher), cbm)
            if status == 200 and eur(at_break) < eur(rate):
                best, billed = at_break, float(min(higher))
        options.append({
            "airline": best["airline"], "airline_code": best["airline_code"], "source": "portal",
            "total_eur": eur(best), "transit_days": best["transit_days"], "billed_kg": billed,
            "rate_id": best["rate_id"], "valid_until": best["valid_until"], "book": "POST /v1/portal/bookings",
        })

    on_portal = {o["airline"] for o in options}
    for t in rows("tariff_lanes.csv"):
        if (t["origin"], t["destination"]) != (origin, dest) or t["airline"] in on_portal:
            continue
        if not (t["valid_from"] <= on.isoformat() <= t["valid_to"]):
            continue
        if t["airline"] not in emails:  # carrier stopped
            continue
        total, billed = tariff_total(t, chargeable)
        options.append({
            "airline": t["airline"], "source": "tariff", "total_eur": total, "transit_days": int(float(t["transit_days"])),
            "billed_kg": billed, "valid_until": t["valid_to"], "book": f"email {emails[t['airline']]}",
        })

    options.sort(key=lambda o: (o["total_eur"], o["transit_days"]))
    return {"origin": origin, "destination": dest, "gross_kg": kg, "volume_cbm": cbm,
            "chargeable_kg": chargeable, "options": options, "not_priced": not_priced}


def demo() -> None:
    lane = {"breaks_kg": "0 45 100 300 500 1000", "rates_per_kg": "5 4 3 2.5 2 1.5",
            "minimum_eur": "80", "fuel_per_kg": "0.5", "security_per_kg": "0.1"}
    assert tariff_total(lane, 10) == (86.0, 10)            # minimum on freight, surcharges on top
    assert tariff_total(lane, 400) == (1240.0, 400)        # own bracket: 400 * (2.5 + 0.6)
    assert tariff_total(lane, 900) == (2100.0, 1000.0)     # anti-weight-break: 1000 kg is cheaper than 900
    print("ok")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--demo"]:
        demo()
    elif len(args) < 4 or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(2)
    else:
        # Tariffs must be valid on the shipping day: the later of today and the ready date.
        on = max(date.today(), date.fromisoformat(args[args.index("--date") + 1])) if "--date" in args else date.today()
        print(json.dumps(quote(args[0].upper(), args[1].upper(), float(args[2]), float(args[3]), on), indent=1))
