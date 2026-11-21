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
TRUCKER_PRICES = {"t1@trucks.test": "Our all-in price: EUR 180.00",
                  "t2@trucks.test": "Base EUR 120 + fuel EUR 30, total EUR 150",
                  "t3@trucks.test": "Sorry, no capacity that day."}
LANES = {"lanes": [
    {"origin": "FRA", "destination": "JFK", "airlines": [{"code": "LH", "name": "Lufthansa Cargo"},
                                                          {"code": "AA", "name": "American Cargo"}]},
    {"origin": "MUC", "destination": "PVG", "airlines": [{"code": "LH", "name": "Lufthansa Cargo"},
                                                          {"code": "CA", "name": "Air China Cargo"}]},
    {"origin": "CGN", "destination": "ORD", "airlines": [{"code": "LH", "name": "Lufthansa Cargo"},
                                                          {"code": "AA", "name": "American Cargo"},
                                                          {"code": "CA", "name": "Air China Cargo"}]},
]}
TRANSIT = {"LH": 2, "AA": 4, "CA": 3}
BREAKS = {"LH": [45, 100, 300, 500], "AA": [100, 500], "CA": [45, 300]}
calls = {"429_sent": False, "rates": 0, "llm": 0, "bookings": 0}
FLOW = {"booked_mail": False, "tracking_mail": False}


def _add_mail(frm, subject, body, in_reply_to):
    MAILS.append({"id": max(m["id"] for m in MAILS if m["id"] < 1000) + 1, "from": frm, "subject": subject,
                  "received_at": NOW, "body": body, "in_reply_to": in_reply_to})


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
            return self.send(200, {"rate_id": "R-" + uuid.uuid4().hex[:12], "airline": a, "airline_code": a,
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
            if req["to"] == "lars@medtech.se" and "Booking reference" not in req["body"] and not FLOW["booked_mail"]:
                FLOW["booked_mail"] = True
                _add_mail("Lars <lars@medtech.se>", "Re: " + req["subject"], "Great, we accept your offer. Please book.", sid)
            elif req["to"] == "lars@medtech.se" and "Booking reference" in req["body"] and not FLOW["tracking_mail"]:
                FLOW["tracking_mail"] = True
                _add_mail("Lars <lars@medtech.se>", "Shipment status?", "Hi, where is our shipment? Any update?", None)
            if req["to"] in TRUCKER_PRICES:
                MAILS.append({"id": max(m["id"] for m in MAILS if m["id"] < 1000) + 1, "from": req["to"],
                              "subject": "Re: " + req["subject"], "received_at": NOW,
                              "body": TRUCKER_PRICES[req["to"]] + "\n\n> " + req["body"].replace("\n", "\n> "),
                              "in_reply_to": sid})
            return self.send(201, {"id": sid, "status": "sent"})
        if u.path == "/v1/portal/bookings":
            calls["bookings"] += 1
            return self.send(201, {"booking_reference": f"BK-{calls['bookings']:05d}", "team_reference": req["team_reference"],
                                   "rate_id": req["rate_id"], "airline": "Lufthansa Cargo", "airline_code": "LH",
                                   "price": "1050.00", "currency": "EUR", "status": "confirmed"})
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
