"""Tiny mock of the 5U API for an end-to-end dry run."""
import json
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

NOW = datetime.now(timezone.utc).isoformat()
MAILS = [
    {"id": 1, "from": "Anna <anna@shipper.de>", "subject": "RFQ FRA-JFK", "received_at": NOW,
     "body": "Hello,\nplease quote 90 kg, 0.3 cbm, 2 pcs.\nBR Anna"},
    {"id": 2, "from": "bob@corp.cn", "subject": "air freight", "received_at": NOW,
     "body": "Need air freight Munich to Shanghai, 200kg, 2 pcs 120x80x100 cm"},
    {"id": 3, "from": "c@x.com", "subject": "Where is my shipment?", "received_at": NOW,
     "body": "Status of AWB 020-12345678 please"},
    {"id": 4, "from": "team1@5u.ai", "subject": "Re: RFQ FRA-JFK", "received_at": NOW, "body": "our own quote"},
    {"id": 5, "from": "Lars <lars@medtech.se>", "subject": "Fastest air freight to ORD", "received_at": NOW,
     "body": "Hi, please quote urgently: pickup at 51447 Bergisch Gladbach on 25.11.2026, deliver to Chicago ORD. "
             "3 pallets, 300 kg, 1.5 cbm. Thanks"},
]
import os as _os
ATTACH = {}  # (mail id, attachment id) -> bytes
_ORION = _os.path.join(_os.path.dirname(__file__), "..", "start-kit", "orion-cargo-air-tariff-2026-11.pdf")


def _attach(mail, aid, filename, ctype, data):
    ATTACH[(mail["id"], aid)] = data
    mail.setdefault("attachments", []).append({"id": aid, "filename": filename, "content_type": ctype, "size": len(data)})


# tariff mail from a rate-sheet airline (not on the portal), then RFQs that use it / test DG / refusal
MAILS.append({"id": 6, "from": "Orion Cargo Air <orion@air.test>", "subject": "New export tariff November",
              "received_at": NOW, "body": "Dear partner,\nplease find our new tariff attached.\nOrion Cargo Air"})
_attach(MAILS[-1], 1, "orion-cargo-air-tariff-2026-11.pdf", "application/pdf", open(_ORION, "rb").read())
MAILS.append({"id": 7, "from": "Carl <carl@x.test>", "subject": "RFQ HAM-DXB", "received_at": NOW,
              "body": "Please quote HAM to DXB, 250 kg, 1 cbm, 2 pcs."})
MAILS.append({"id": 8, "from": "Klaus <klaus@toys.test>", "subject": "Air freight quote to JFK", "received_at": NOW,
              "body": "Please quote FRA to JFK, 300 kg, 1.2 cbm, 3 pcs of industrial adhesives. "
                      "The goods are general cargo, non-dangerous, packing specification attached."})
_attach(MAILS[-1], 1, "packing_spec.txt", "text/plain",
        b"PACKING SPECIFICATION\n3 pcs drums, 300 kg\nContent: ADHESIVES, UN 1133, Class 3, PG II\n")
TRUCKER_PRICES = {"t1@trucks.test": "Our all-in price: EUR 180.00",
                  "t2@trucks.test": "Base EUR 120 + fuel EUR 30, total EUR 150",
                  "t3@trucks.test": "Sorry, no capacity that day."}
LANES = {"lanes": [
    {"origin": "FRA", "destination": "JFK", "airlines": [{"code": "LH", "name": "Lufthansa Cargo"},
                                                          {"code": "AA", "name": "American Cargo"}]},
    {"origin": "MUC", "destination": "PVG", "airlines": [{"code": "LH", "name": "Lufthansa Cargo"},
                                                          {"code": "CA", "name": "Air China Cargo"}]},
    {"origin": "HAM", "destination": "DXB", "airlines": [{"code": "LH", "name": "Lufthansa Cargo"}]},
    {"origin": "CGN", "destination": "ORD", "airlines": [{"code": "LH", "name": "Lufthansa Cargo"},
                                                          {"code": "AA", "name": "American Cargo"},
                                                          {"code": "CA", "name": "Air China Cargo"}]},
]}
TRANSIT = {"LH": 2, "AA": 4, "CA": 3}
BREAKS = {"LH": [45, 100, 300, 500], "AA": [100, 500], "CA": [45, 300]}
calls = {"429_sent": False, "rates": 0, "llm": 0, "bookings": 0}
FLOW = {"booked_mail": False, "tracking_mail": False}


ISSUED: dict = {}   # rate_id -> rate
BOOKED: dict = {}   # rate_id -> booking
MODE = {"expire_first": True, "booking_down_for": "FRA"}
NAMES = {"LH": "Lufthansa Cargo", "AA": "American Cargo", "CA": "Air China Cargo"}


def _refs(mid):
    m = next((x for x in MAILS if x["id"] == mid), None)
    return list(m.get("references") or []) if m else []


def _add_mail(frm, subject, body, in_reply_to, references=None):
    MAILS.append({"id": max(m["id"] for m in MAILS if m["id"] < 1000) + 1, "from": frm, "subject": subject,
                  "received_at": datetime.now(timezone.utc).isoformat(), "body": body,
                  "in_reply_to": in_reply_to, "references": references or []})


def price(airline, w):
    if airline == "LH":
        return (5.0 if w < 100 else 3.5) * w, "EUR"
    if airline == "AA":
        return 4.2 * w, "EUR"
    return 3.0 * w, "USD"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("X-API-Key") != "test-key":
            return self.send(401, {"detail": "bad key"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p == "/v1/me":
            return self.send(200, {"team": "t1", "mailbox": "team1@5u.ai"})
        if p == "/v1/mailbox/messages":
            since = int(q.get("since_id", 0))
            items = [{k: v for k, v in m.items() if k != "body"} for m in MAILS if m["id"] > since]
            return self.send(200, {"messages": items, "next_since_id": max([since] + [m["id"] for m in items])})
        if p.startswith("/v1/mailbox/messages/") and "/attachments/" in p:
            parts = p.split("/")
            data = ATTACH.get((int(parts[4]), int(parts[6])))
            if data is None:
                return self.send(404, {"detail": "no attachment"})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(data)
            return
        if p.startswith("/v1/mailbox/messages/"):
            try:
                mid = int(p.rsplit("/", 1)[1])
            except ValueError:
                return self.send(422, {"detail": [{"loc": ["path", "message_id"], "msg": "int", "type": "int_parsing"}]})
            return self.send(200, next(m for m in MAILS if m["id"] == mid))
        if p == "/v1/portal/ports":
            return self.send(200, LANES)
        if p == "/v1/portal/brackets":
            a = q["airline"]
            return self.send(200, {"airline": a, "airline_code": a, "breaks_kg": BREAKS.get(a, [])})
        if p == "/v1/portal/rates":
            calls["rates"] += 1
            a = q["airline"]
            if a == "AA" and not calls["429_sent"]:
                calls["429_sent"] = True
                return self.send(429, {"detail": "slow down"}, {"Retry-After": "1"})
            w = float(q["weight_kg"]); v = float(q.get("volume_cbm", 0))
            cw = max(w, v * 167)
            total, cur = price(a, cw)
            rid = "R-" + uuid.uuid4().hex[:12]
            ISSUED[rid] = {"origin": q["origin"], "airline_code": a, "airline_name": NAMES.get(a, a),
                           "total": f"{total:.2f}", "currency": cur}
            return self.send(200, {"rate_id": rid, "airline": NAMES.get(a, a), "airline_code": a,
                                   "origin": q["origin"], "destination": q["destination"],
                                   "weight_kg": str(w), "chargeable_weight_kg": str(round(cw, 1)),
                                   "total": f"{total:.2f}", "currency": cur, "transit_days": TRANSIT.get(a, 2),
                                   "valid_until": "2026-11-22T00:00:00Z", "dataset_version": "v1",
                                   "fx_eur_per_usd": "0.92"},
                             {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "55"})
        if p == "/v1/gateway/models":
            return self.send(200, {"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4.1-nano"}]})
        return self.send(404, {"detail": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/v1/mailbox/messages":
            calls.setdefault("sent", []).append(req)
            sid = 1000 + len(calls["sent"])
            to, body = req["to"], req["body"]
            irt = req.get("in_reply_to")
            refs = ([*_refs(irt), irt] if irt else []) + [sid]
            if to == "lars@medtech.se" and "Quote reference" in body and not FLOW["booked_mail"]:
                FLOW["booked_mail"] = True  # customer accepts, quoting our mail below
                _add_mail("Lars <lars@medtech.se>", "Re: " + req["subject"].removeprefix("Re: "),
                          "Great, we accept your offer. Please book.\n\nOn Sat, Air Freight Desk wrote:\n"
                          + body, sid, refs)
            elif to == "lars@medtech.se" and "Booking reference" in body and not FLOW["tracking_mail"]:
                FLOW["tracking_mail"] = True
                _add_mail("Lars <lars@medtech.se>", "Shipment status?", "Hi, where is our shipment? Any update?", None, [])
            elif to == "anna@shipper.de" and "Quote reference" in body and not FLOW.get("anna_accept"):
                FLOW["anna_accept"] = True
                _add_mail("Anna <anna@shipper.de>", "Re: RFQ FRA-JFK", "Please book it. Thanks, Anna", sid, refs)
            elif to == "anna@shipper.de" and "Booking reference" in body and not FLOW.get("anna_track"):
                FLOW["anna_track"] = True
                _add_mail("Anna <anna@shipper.de>", "Status of our shipment FRA to JFK, 90 kg",
                          "Hello, could you tell us the status of our shipment FRA to JFK? Anna", None, [])
            elif to == "carl@x.test" and "Quote reference" in body and not FLOW.get("carl"):
                FLOW["carl"] = True
                _add_mail("Carl <carl@x.test>", "Re: RFQ HAM-DXB", "OK, please book it.", sid, refs)
            elif to == "carl@x.test" and "Booking reference" in body and not FLOW.get("carl_thanks"):
                FLOW["carl_thanks"] = True  # thank-you note: must NOT trigger another confirmation
                _add_mail("Carl <carl@x.test>", "Re: RFQ HAM-DXB", "Thanks for confirming the booking. Best, Carl", sid, refs)
            elif to == "bob@corp.cn" and "Quote reference" in body and not FLOW.get("bob"):
                FLOW["bob"] = True
                _add_mail("bob@corp.cn", "Re: air freight", "Thank you for your offer, but we will not go ahead with it.",
                          sid, refs)
            elif to == "klaus@toys.test" and "Dangerous Goods" in body and not FLOW.get("klaus_docs"):
                FLOW["klaus_docs"] = True
                _add_mail("Klaus <klaus@toys.test>", "Re: Air freight quote to JFK",
                          "Attached our DG documents.", sid, refs)
                _attach(MAILS[-1], 1, "DGD_UN1133.txt", "text/plain",
                        b"Shipper's Declaration for Dangerous Goods\nUN 1133 Adhesives, Class 3, PG II, 3 drums\n")
            elif to == "klaus@toys.test" and "Quote reference" in body:
                FLOW["klaus_quoted_in_reply_to"] = irt
            elif to == "orion@air.test" and "Rate ID:" in body:
                rid = next(l.split(":", 1)[1].strip() for l in body.splitlines() if l.startswith("Rate ID:"))
                _add_mail("Orion Cargo Air <orion@air.test>", "Booking BK-ORION001 confirmed",
                          f"Booking BK-ORION001 confirmed for rate {rid}.", sid, refs)
            elif to == "lh@air.test" and "Rate ID:" in body:  # booking by e-mail -> airline confirms
                rid = next(l.split(":", 1)[1].strip() for l in body.splitlines() if l.startswith("Rate ID:"))
                ref = next(l.split(":", 1)[1].strip() for l in body.splitlines() if l.startswith("Our reference:"))
                _add_mail("Lufthansa Cargo <lh@air.test>", "Booking BK-EMAIL001 confirmed",
                          f"Booking BK-EMAIL001 confirmed.\nRate: {rid}\nYour reference: {ref}\nReady to ship.", sid, refs)
            if to in TRUCKER_PRICES:
                _add_mail(to, "Re: " + req["subject"], TRUCKER_PRICES[to] + "\n\n> " + body.replace("\n", "\n> "),
                          sid, refs)
            return self.send(201, {"id": sid, "status": "sent", "to": to, "subject": req["subject"]})
        if u.path == "/v1/portal/bookings":
            calls["bookings"] += 1
            rid, tref = req["rate_id"], req["team_reference"]
            if rid not in ISSUED:
                return self.send(404, {"detail": "unknown rate_id"})
            if MODE.get("booking_down_for") and ISSUED[rid]["origin"] == MODE["booking_down_for"]:
                return self.send(503, {"detail": "Booking system unavailable. Book by e-mail with the airline "
                                                 "instead: send the rate_id and your reference."})
            if MODE.get("expire_first") and not FLOW.get("expired_once"):
                FLOW["expired_once"] = True
                return self.send(410, {"detail": "rate expired"})
            if rid in BOOKED:
                if BOOKED[rid]["team_reference"] != tref:
                    return self.send(409, {"detail": "rate_id already booked under another team_reference"})
                return self.send(200, BOOKED[rid])
            r = ISSUED[rid]
            ref = f"BK-{calls['bookings']:08d}"
            BOOKED[rid] = {"booking_reference": ref, "team_reference": tref, "rate_id": rid,
                           "airline": r["airline_name"], "airline_code": r["airline_code"],
                           "price": r["total"], "currency": r["currency"], "status": "booked"}
            _add_mail(f"{r['airline_name']} <lh@air.test>", f"Booking {ref} confirmed",
                      f"Booking {ref} confirmed. Rate {rid}, your reference {tref}.", None, [])
            return self.send(201, BOOKED[rid])
        if u.path == "/v1/gateway/chat/completions" and "response_format" not in req:
            calls["llm"] += 1
            draft = req["messages"][-1]["content"]
            text = "Hi,\n\nthanks a lot for reaching out!\n\n" + draft.split("\n", 2)[-1]
            return self.send(200, {"id": "x", "object": "chat.completion", "created": 0, "model": req["model"],
                                   "choices": [{"index": 0, "finish_reason": "stop",
                                                "message": {"role": "assistant", "content": text}}],
                                   "usage": {"prompt_tokens": 150, "completion_tokens": 120, "total_tokens": 270}})
        if u.path == "/v1/gateway/chat/completions":
            calls["llm"] += 1
            content = json.dumps({"category": "rfq", "shipments": [
                {"origin": "MUC", "destination": "PVG", "weight_kg": 200, "volume_cbm": 1.0, "pieces": 2}]})
            return self.send(200, {"id": "x", "object": "chat.completion", "created": 0, "model": req["model"],
                                   "choices": [{"index": 0, "finish_reason": "stop",
                                                "message": {"role": "assistant", "content": content}}],
                                   "usage": {"prompt_tokens": 180, "completion_tokens": 40, "total_tokens": 220}})
        return self.send(404, {"detail": "not found"})


def start(port=8765):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
