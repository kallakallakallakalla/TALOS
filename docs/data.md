# Local data (start kit)

Source: `https://api.start-hack.5u.ai/kit/start-kit.zip` (with the API key). Unpacked into `data/start-kit/`.

## carrier-list.csv

Columns: `name,kind,email,serves`.

### Airlines (29)

For an airline, `serves` lists the destination IATA codes it serves. The 29 airlines fall into three groups that **don't overlap**:

| Group | Airlines | How to price | How to book |
|---|---|---|---|
| Portal (15) | Aurelia, Borealis, Fjordline, Gannet, Ibisair, Juniper, Lumen, Meridian, Nova Atlas, Polaris, Riverbend, Solace, Tamarind, Ultraviolet, Xenon | `GET /v1/portal/rates` | `POST /v1/portal/bookings` |
| Tariff files (10) | Cobalt Skylift, Danube Wings, Elbflug, Helix, Kestrel, Orion, Quetzal, Vega, Wayzata, Yellowtail | local DB built from the files below | unknown; probably by email to the airline (open question) |
| Email-only (4) | Zephyr Steppe, Himalaya Bridge, Lagoon Isles, Pampero | email the airline; they serve only ASU, KTM, PPT, TNR, ULN | email |

Each airline's address is `<slug>@hackloop.mail`, e.g. `yellowtail-air-freight@hackloop.mail`.

### Truckers (22)

For a trucker, `serves` lists **2-digit German postal-code prefixes**, not airports. Examples:

- `Grautal Trucking` serves only zone 45.
- `Neckarstrom Kurier` serves zones 24 55 65 73 80 81 85 86 93.

Every trucker zone appears in `pickup_zones.csv`, and every pickup zone has at least one trucker.

## pickup_zones.csv

Columns: `zone,area,airport`. Each of the 32 zones maps to one of 6 airports:

| Airport | Zones |
|---|---|
| FRA | 60 Frankfurt, 61 Bad Homburg, 63 Offenbach, 64 Darmstadt, 65 Wiesbaden, 55 Mainz, 35 Giessen, 36 Fulda |
| MUC | 80 Munich, 81 Munich South, 84 Landshut, 85 Ingolstadt, 86 Augsburg, 93 Regensburg |
| DUS | 40 Duesseldorf, 41 Moenchengladbach, 42 Wuppertal, 44 Dortmund, 45 Essen, 47 Duisburg |
| HAM | 20 Hamburg, 21 Lueneburg, 22 Hamburg North, 24 Kiel, 28 Bremen |
| STR | 70 Stuttgart, 71 Boeblingen, 72 Tuebingen, 73 Esslingen |
| CGN | 50 Cologne, 51 Bergisch Gladbach, 53 Bonn |

Lookup: take the first 2 digits of the customer's postal code. That gives the **airport** (from this file) and the **truckers to ask** (every trucker whose `serves` contains the zone).

Example: 51447 Bergisch Gladbach → zone 51 → CGN. Ask Maintal, Elbkante, Westfalenroute and Bodenseefracht. These are exactly the four truckers that were asked in round 1.

Each zone maps to exactly one airport, so there is no "nearest of several airports" choice. Open question: what to do with a zone that isn't in the table, e.g. 90 Nuremberg. Possible answers are the geographically nearest airport, or asking the customer.

## Airline tariff files (the local DB)

All 10 files are November 2026 tariffs (valid 2026-11-01 to 2026-11-30) in EUR, for export lanes from the 6 German airports. They say the same thing in different layouts, so a parser per format is needed (or one LLM extraction, done once). The terms are identical in every file:

> All rates are per kg of chargeable weight, in EUR. Chargeable weight is the higher of actual weight and volume weight (1 cbm = 167 kg). Fuel and security surcharges are charged per kg of the weight the freight is billed on. The minimum charge applies to the freight before surcharges.

| File | Format quirks |
|---|---|
| `yellowtail-…txt` | Fixed-width text, `EUR 5.92` values, breaks `-45 +45 +100 +250 +500 +1000 +2000`, surcharges listed below the table |
| `elbflug-…xlsx` | Grouped by "Origin: X", **comma decimals**, valid dates as **Excel serials** (46327 = 2026-11-01), breaks 0/45/100/300/500/1000 (**no 2000**) |
| `kestrel-…xlsx` | Grouped by origin, transit written as `D+4`, comma decimals, fuel and security as columns in the same row |
| `vega-…xlsx` | "Cologne (CGN)" naming, dot decimals, float noise (`83.73999999999999`), breaks up to 2000 |
| `wayzata-…xlsx` | `CGN Cologne > DEL Delhi` lane strings, `&gt;` escapes, "2 days", no 2000 break |
| `quetzal-…docx` | Word table, comma decimals, breaks 0…2000 |
| `cobalt-…pdf` | One block per lane (Transit, Minimum, from X kg…, Fuel, Security) |
| `helix-…pdf` | Wide table, `D+4`, breaks include 250 |
| `orion-…pdf` | One block per lane, "2 days", comma decimals, breaks up to 1000; surcharges at the end of the file |
| `danube-…p1.jpg` / `p2.jpg` | **Scanned images**, slightly rotated. Page 1 has the rates, page 2 the surcharges. Needs OCR or an LLM vision model (one-off) |

**Weight breaks differ per airline.** Some use 250 and others 300; some stop at 1000 and others go to 2000. Store the breaks per lane and don't assume a fixed set.

Proposed DB schema, mirroring the portal response:

```text
tariff(airline, airline_code?, origin, destination, transit_days, minimum_eur,
       breaks_kg[], rates_per_kg[], fuel_per_kg, security_per_kg,
       currency, valid_from, valid_to, source_file, dataset_version)
```

Airline tariff mails can arrive in the mailbox, possibly with updated tariffs as attachments. Their payloads go into the same table with a newer `dataset_version`.

## Pricing formula

```text
chargeable = max(gross_kg, cbm * 167)
for every bracket b with break_kg >= chargeable's bracket (anti-weight-break):
    billed     = max(chargeable, break_kg(b))
    freight    = max(minimum, billed * rate(b))
    surcharges = billed * (fuel + security)
    total(b)   = freight + surcharges
air_total = min over b of total(b)
all_in    = air_total + trucker_price (if pickup was requested) + margin
```

Two checks against real portal quotes:

- XC CGN→ORD, 679 kg, 4.19 cbm: chargeable 699.73 kg, total 1483.43 → 2.12 €/kg all-in.
- Round 1 used anti-weight-break: 676 kg and 5.79 cbm give 966.93 kg chargeable, and the quote billed 1000 kg. That quote was rejected for a different reason (trucking was missing).
