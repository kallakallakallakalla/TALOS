import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from quote_bot.extract import (Mail, parse_number, rules_extract, strip_quoted, find_weight,
                               volume_from_dims, parse_llm_json, merge_rules_into_llm)

CODES = {"FRA", "JFK", "MUC", "PVG", "HKG", "LHR", "ORD", "CAN", "DXB", "SIN"}


def mail(subject, body, mid=1, reply=False, refs=None):
    d = {"id": mid, "subject": subject, "body": body, "from": "c@x.com",
         "received_at": "2026-11-21T14:00:00Z"}
    if refs:
        d["references"] = refs
    return Mail.from_api(d)


cases = [
    ("RFQ FRA-JFK", "Hi, please quote 350 kg, 2.1 cbm, 3 pcs. Thanks", ("rfq", "FRA", "JFK", 350, 2.1)),
    ("Quote request", "Origin: Frankfurt (FRA)\nDestination: New York (JFK)\nGross weight: 1.250 kg\nVolume: 4,5 cbm",
     ("rfq", "FRA", "JFK", 1250, 4.5)),
    ("Anfrage", "Bitte Angebot von MUC nach PVG, 2 Paletten 120x80x100 cm, Gesamtgewicht 480 kg",
     ("rfq", "MUC", "PVG", 480, 1.92)),
    ("rates please", "Shipment from MUC to HKG, 800 lbs, 3 pcs 100x100x100cm", ("rfq", "MUC", "HKG", 362.874, 3.0)),
    ("New request", "Need air freight Munich to Shanghai, 200kg", ("needs_llm",)),
    ("Re: RFQ FRA-JFK 350kg", "We accept your offer, please book.\n\nOn Sat, 5U wrote:\n> FRA-JFK 350 kg total 1200 EUR",
     ("booking",)),
    ("Where is my shipment?", "Hi, what's the status of AWB 020-12345678?", ("tracking",)),
    ("RFQ", "Two shipments: FRA-JFK 100 kg and MUC-HKG 200 kg", ("needs_llm",)),
    ("RFQ LHR → DXB", "4 boxes à 50 kg, total 200 kg, 1.2 m3", ("rfq", "LHR", "DXB", 200, 1.2)),
    ("Please send your quote to us by Friday", "Shipment from SIN to ORD, chargeable weight 500 kg, gross weight 420 kg, 2.0 cbm",
     ("rfq", "SIN", "ORD", 420, 2.0)),
]

ok = 0
for subj, body, exp in cases:
    ex = rules_extract(mail(subj, body), CODES)
    if exp[0] == "needs_llm":
        good = ex.needs_llm
    elif exp[0] != "rfq":
        good = ex.category == exp[0] and not ex.needs_llm
    else:
        s = ex.shipments[0] if ex.shipments else None
        good = (ex.category == "rfq" and s and s.origin == exp[1] and s.destination == exp[2]
                and abs(s.weight_kg - exp[3]) < 0.01 and abs((s.volume_cbm or 0) - exp[4]) < 0.001)
    ok += bool(good)
    detail = [(s.origin, s.destination, s.weight_kg, s.volume_cbm, s.pieces, s.flags) for s in ex.shipments]
    print("OK  " if good else "FAIL", subj[:40], "->", ex.category, ex.method, ex.needs_llm, detail)
print(f"{ok}/{len(cases)} passed")

assert parse_number("1,250.5") == 1250.5
assert parse_number("1.250,5") == 1250.5
assert parse_number("2,5") == 2.5
assert parse_number("1.250") == 1250
assert parse_number("1.250", thousands_3digit=False) == 1.25
assert parse_number("0.500") == 0.5

# LLM json parse + merge
llm = parse_llm_json('{"category":"rfq","shipments":[{"origin":"muc","destination":"PVG","weight_kg":"200","volume_cbm":1.0,"pieces":null}]}', CODES, 123)
m = mail("New request", "Need air freight Munich to Shanghai, 200kg, 2 pcs 120x80x100 cm")
merge_rules_into_llm(llm, m)
s = llm.shipments[0]
print("LLM merge:", s)
assert s.origin == "MUC" and s.weight_kg == 200 and abs(s.volume_cbm - 1.92) < 1e-6 and s.pieces == 2
print("asserts ok")
