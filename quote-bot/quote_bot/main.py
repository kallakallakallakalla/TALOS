"""Entry point: python -m quote_bot [--dump | --once | --message-id N] [--no-llm] [--skip-backlog]"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .api import ApiError, FiveUClient, messages_from_poll, next_since_id
from .config import Config, load_config
from .db import DB
from .extract import (
    Extraction,
    LLMExtractor,
    Mail,
    has_body,
    load_carriers,
    merge_rules_into_llm,
    resolve_model,
    rules_extract,
    strip_quoted,
    EMAIL_ANY_RE,
)
from .pickup import PickupDesk, PickupRequest, is_fastest, parse_price, ref_ids
from .pricing import BracketCache, LaneMap, LotQuote, Pricer, grand_total, total_in_eur
from .maillog import log_incoming
from .reply import address, booking_draft, re_subject, send_quote, tracking_draft
from .writer import Writer

log = logging.getLogger("quote_bot")
BAR = "━" * 78


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _fmt_num(v: Any, digits: int = 2) -> str:
    if v is None or v == "":
        return "?"
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return str(v)
    return f"{d:,.{digits}f}"


def _own_addresses(me: Any) -> set[str]:
    out: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str) and "@" in x and " " not in x.strip():
            out.add(x.strip().lower())

    walk(me)
    return out


def _sort_key(r) -> tuple:
    """Errors last; mixed currencies sorted by EUR equivalent where possible."""
    if r.total is None:
        return (1, Decimal(0))
    return (0, total_in_eur(r) or r.total)


def print_non_rfq(mail: Mail, ex: Extraction) -> None:
    tok = f" · llm {ex.llm_tokens} tok" if ex.llm_tokens else ""
    flags = f" · {'; '.join(ex.flags)}" if ex.flags else ""
    print(f"MSG {mail.id} · {ex.category.upper()} · {ex.method}{tok} · {mail.subject!r}{flags}", flush=True)


def print_rfq(mail: Mail, ex: Extraction, lots: list[LotQuote], elapsed: float, left: float) -> dict:
    tok = f" · llm {ex.llm_tokens} tok" if ex.llm_tokens else ""
    print(f"\n{BAR}\nMSG {mail.id} · RFQ · {ex.method}{tok}")
    print(f"From:     {mail.sender}")
    print(f"Subject:  {mail.subject}")
    payload_lots = []
    for i, lot in enumerate(lots, 1):
        s = lot.shipment
        print(
            f"Lot {i}:    {s.origin or '?'} → {s.destination or '?'} | {_fmt_num(s.weight_kg, 1)} kg"
            f" | {_fmt_num(s.volume_cbm, 3)} cbm | {s.pieces if s.pieces is not None else '?'} pcs"
        )
        rows = sorted(lot.results, key=_sort_key)
        if rows:
            print(f"  {'Airline':<30} {'Quoted kg':>10} {'Chg kg':>9} {'Total':>13} {'Cur':<4} {'Days':>4}  Note")
        for r in rows:
            if r.rate is None:
                print(f"  {r.airline.label[:30]:<30} {'':>10} {'':>9} {'':>13} {'':<4} {'':>4}  ERROR {r.error}")
                continue
            note = []
            if r.break_applied:
                note.append(f"break {r.next_break} kg cheaper")
            elif r.next_break:
                note.append(f"break {r.next_break} kg not cheaper")
            if r is lot.best:
                note.insert(0, "★ BEST")
            print(
                f"  {r.airline.label[:30]:<30} {_fmt_num(r.rate.get('weight_kg'), 1):>10}"
                f" {_fmt_num(r.rate.get('chargeable_weight_kg'), 1):>9} {_fmt_num(r.total):>13}"
                f" {r.currency:<4} {str(r.rate.get('transit_days', '?')):>4}  {', '.join(note)}"
            )
        if lot.best:
            b = lot.best.rate
            print(
                f"  ★ BEST: {lot.best.airline.label} {_fmt_num(lot.best.total)} {lot.best.currency}"
                f" | rate_id {b.get('rate_id')} | valid until {b.get('valid_until')}"
                f" | dataset {b.get('dataset_version')}"
            )
            if lot.pickup:
                gt, gcur, pconv = grand_total(lot)
                print(
                    f"  + PICKUP {lot.pickup['zip']} {lot.pickup['city']} → {lot.pickup['airport']}:"
                    f" {lot.pickup['price_eur']} EUR ({lot.pickup['trucker']}) | TOTAL {gt} {gcur}"
                )
        flags = s.flags + lot.flags
        if flags:
            print(f"  Flags: {'; '.join(flags)}")
        payload_lots.append(
            {
                "lot": i,
                "shipment": s.to_dict(),
                "best": (
                    {
                        "airline": lot.best.airline.label,
                        "airline_code": lot.best.airline.code,
                        "total": str(lot.best.total),
                        "currency": lot.best.currency,
                        "rate_id": lot.best.rate.get("rate_id"),
                        "quoted_weight_kg": lot.best.rate.get("weight_kg"),
                        "chargeable_weight_kg": lot.best.rate.get("chargeable_weight_kg"),
                        "transit_days": lot.best.rate.get("transit_days"),
                        "valid_until": lot.best.rate.get("valid_until"),
                        "break_applied": lot.best.break_applied,
                    }
                    if lot.best
                    else None
                ),
                "rates": [
                    {"airline": r.airline.code, "total": str(r.total) if r.total is not None else None,
                     "currency": r.currency, "rate_id": (r.rate or {}).get("rate_id"), "error": r.error}
                    for r in rows
                ],
                "flags": flags,
                "pickup": lot.pickup,
                "grand_total": str(grand_total(lot)[0]) if lot.best else None,
            }
        )
    if ex.flags:
        print(f"Flags:    {'; '.join(ex.flags)}")
    window = f"{left:.0f} s left in window" if left > 0 else f"{-left:.0f} s PAST window"
    print(f"Time:     {elapsed:.1f} s processing · {window}")
    payload = {
        "message_id": mail.id,
        "sender": mail.sender,
        "subject": mail.subject,
        "category": ex.category,
        "method": ex.method,
        "llm_tokens": ex.llm_tokens,
        "lots": payload_lots,
        "flags": ex.flags,
        "elapsed_s": round(elapsed, 2),
    }
    print("QUOTE_JSON " + json.dumps(payload, ensure_ascii=False, default=str), flush=True)
    return payload


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
class Bot:
    def __init__(self, cfg: Config, client: FiveUClient, db: DB, use_llm: bool = True,
                 reply: bool = False):
        self.cfg = cfg
        self.client = client
        self.db = db
        self.lanes = LaneMap(client, cfg.ports_refresh_s)
        self.pricer = Pricer(client, self.lanes, BracketCache(client, cfg.brackets_ttl_s), cfg.rate_workers)
        self.llm: LLMExtractor | None = None
        self.use_llm = use_llm
        self.own: set[str] = set()
        self.reply = reply
        self.writer = Writer(None)
        self.team = "team"
        carriers_path = os.getenv("CARRIER_LIST", "start-kit/carrier-list.csv")
        self.carriers = load_carriers(carriers_path)
        self.pickup = PickupDesk(os.getenv("PICKUP_ZONES", "start-kit/pickup_zones.csv"), carriers_path)

    def start(self) -> None:
        me = self.client.me()
        self.own = _own_addresses(me)
        if isinstance(me, dict) and me.get("team"):
            self.team = str(me["team"]).upper()
        self.client.on_sent = lambda resp, payload: (
            self.db.save_sent(int(resp["id"]), payload.get("to", ""), payload.get("subject", ""),
                              payload.get("in_reply_to"))
            if isinstance(resp, dict) and isinstance(resp.get("id"), int) else None
        )
        log.info("me: %s", json.dumps(me, default=str)[:300])
        self.lanes.refresh()
        if self.use_llm:
            model = resolve_model(self.client, self.cfg.llm_model)
            self.llm = LLMExtractor(self.cfg.base_url, self.cfg.api_key, model, self.cfg.llm_max_chars)
            self.writer = Writer(self.llm)
            log.info("LLM fallback model: %s | humanize mails: %s", model, self.writer.enabled)
        else:
            log.info("LLM disabled (--no-llm)")

    def _deadline(self, mail: Mail) -> tuple[float, float]:
        """(monotonic deadline for retries, seconds left in the 6-min window)."""
        age = 0.0
        if mail.received_at:
            age = max(0.0, (datetime.now(timezone.utc) - mail.received_at).total_seconds())
        left_window = self.cfg.quote_window_s - age
        budget = max(left_window - self.cfg.safety_margin_s, 10.0)  # always allow a short try
        return time.monotonic() + budget, left_window

    def process(self, summary: dict) -> None:
        t0 = time.monotonic()
        data = summary if has_body(summary) else self.client.message(int(summary["id"]))
        if isinstance(data, dict) and "id" not in data and isinstance(data.get("message"), dict):
            data = data["message"]  # tolerate {"message": {...}} wrapper
        mail = Mail.from_api(data)
        try:
            log_incoming(mail)
        except OSError as exc:
            log.warning("mail log failed: %s", exc)
        deadline, left_window = self._deadline(mail)

        if mail.sender and any(addr in mail.sender.lower() for addr in self.own):
            self._save(mail, Extraction("own", method="rules"), "skipped")
            return

        self.lanes.ensure_fresh()
        pre_info = self.pickup.detect(f"{mail.subject}\n{strip_quoted(mail.body)}")
        ex = rules_extract(mail, self.lanes.codes, self.carriers,
                           origin_hint=pre_info.airport if pre_info else None)
        if ex.needs_llm:
            if self.llm is None:
                ex = Extraction("unknown", method="none", flags=["rules failed, LLM disabled"])
            else:
                try:
                    ex = self.llm.extract(mail, self.lanes.codes)
                    merge_rules_into_llm(ex, mail)
                except Exception as exc:  # noqa: BLE001
                    log.exception("LLM extraction failed for %s", mail.id)
                    ex = Extraction("unknown", method="llm", flags=[f"LLM error: {exc}"])

        if ex.category == "rfq" and ex.method == "llm" and any(
            s.origin and s.origin == s.destination for s in ex.shipments
        ):
            ex.flags.append("origin == destination: not a valid air RFQ")
            ex.category = "other"

        if ex.category == "carrier_quote":
            self.handle_bid(mail, ex)
            print_non_rfq(mail, ex)
            self._save(mail, ex, "done")
            self.check_pending()
            return

        if ex.category == "booking":
            status = self.handle_booking(mail, ex, deadline)
            print_non_rfq(mail, ex)
            self._save(mail, ex, status)
            return

        if ex.category == "tracking":
            status = self.handle_tracking(mail, ex, deadline)
            print_non_rfq(mail, ex)
            self._save(mail, ex, status)
            return

        if ex.category != "rfq":
            print_non_rfq(mail, ex)
            self._save(mail, ex, "done")
            return

        if not ex.shipments:
            ex.flags.append("RFQ without extractable shipment")
        text = f"{mail.subject}\n{strip_quoted(mail.body)}"
        info = self.pickup.detect(text)
        fastest = is_fastest(text)
        for s in ex.shipments:
            if s.volume_cbm is None:
                s.volume_cbm = 0.0
                s.flags.append("volume missing, assumed 0")
            if fastest:
                s.priority = "fastest"
            if info and s.origin != info.airport:
                s.flags.append(f"origin {s.origin} -> {info.airport} from pickup zip {info.zip}")
                s.origin = info.airport

        window_end = time.monotonic() + left_window
        req = None
        if info and ex.shipments:
            truckers = self.pickup.truckers_for(info.zone)
            if not truckers:
                ex.flags.append(f"no trucker serves zone {info.zone}: pickup not included")
            elif not self.reply:
                ex.flags.append("pickup detected: trucker requests need --reply")
            else:
                now = time.monotonic()
                req = PickupRequest(
                    mail=mail, ex=ex, lots=[], info=info, deadline=deadline, window_end=window_end,
                    wait_until=min(now + self.pickup.wait_s, deadline - 5), t0=t0,
                )
                self.pickup.send_requests(self.client, req, ex.shipments[0], self.writer)  # truckers first

        lots = [self.pricer.quote(s, deadline) for s in ex.shipments]
        if req is not None:
            req.lots = lots
            print(
                f"\n{BAR}\nMSG {mail.id} · RFQ · pickup {info.zip} {info.city} → {info.airport}"
                f" · asked {len(req.asked)} truckers, waiting ≤{req.wait_until - time.monotonic():.0f} s",
                flush=True,
            )
            self._save(mail, ex, "waiting_pickup")
            return
        self.finish(mail, ex, lots, deadline, window_end, t0)

    def finish(self, mail: Mail, ex: Extraction, lots: list[LotQuote], deadline: float,
               window_end: float, t0: float) -> None:
        elapsed = time.monotonic() - t0
        print_rfq(mail, ex, lots, elapsed, window_end - time.monotonic())
        for i, lot in enumerate(lots, 1):
            rate = dict(lot.best.rate) if lot.best else None
            if rate is not None and lot.pickup:
                rate["pickup"] = lot.pickup
                rate["grand_total"] = str(grand_total(lot)[0])
            self.db.save_quote(
                mail.id, i, lot.shipment.to_dict(), rate,
                lot.best.airline.label if lot.best else "",
                lot.best.break_applied if lot.best else False,
            )
        all_flags = ex.flags + [f for lot in lots for f in lot.shipment.flags + lot.flags]
        status = "quoted" if lots and all(lot.best for lot in lots) else "incomplete"
        if self.reply and status == "quoted":
            try:
                sent = send_quote(self.client, mail, lots, max(deadline, time.monotonic() + 10), self.writer)
                status = "replied"
                print(f"→ REPLIED to {address(mail.sender)} (in_reply_to {mail.id}): "
                      f"{json.dumps(sent, default=str)[:200]}", flush=True)
            except ApiError as exc:
                all_flags.append(f"reply failed: {exc}")
                print(f"✗ REPLY FAILED for {mail.id}: {exc}", flush=True)
        elif self.reply:
            print(f"✗ NO REPLY for {mail.id}: not all lots priced", flush=True)
        self._save(mail, ex, status, all_flags)

    # -- pickup bids ------------------------------------------------------------
    def handle_bid(self, mail: Mail, ex: Extraction) -> None:
        req = self.pickup.match(mail)
        if req is None:
            late = self.pickup.match(mail, include_done=True)
            ex.flags.append(f"late bid, quote for MSG {late.mail.id} already sent" if late
                            else "bid for a request not sent by this bot (ignored)")
            return
        body = strip_quoted(mail.body) or mail.body
        price, ambiguous = parse_price(body)
        if ambiguous and self.llm is not None:
            try:
                p2, tok = self.llm.extract_price(body)
                ex.llm_tokens += tok
                if p2 is not None:
                    price = Decimal(str(p2))
            except Exception as exc:  # noqa: BLE001
                log.warning("LLM price extraction failed: %s", exc)
        m = EMAIL_ANY_RE.search(mail.sender or "")
        sender = m.group(0).lower() if m else mail.sender
        req.bids[sender] = price
        ex.flags.append(f"pickup bid {price if price is not None else 'declined'} EUR for MSG {req.mail.id}"
                        f" ({len(req.bids)}/{len(req.asked)})")

    # -- booking + tracking -------------------------------------------------------
    def find_rfq(self, mail: Mail) -> int | None:
        for r in ref_ids(mail):
            parent = self.db.sent_parent(r)
            for cand in (parent, r):
                if cand is not None and self.db.quotes_for(cand):
                    return cand
        email = address(mail.sender)
        subj = re.sub(r"(?i)^\s*((re|aw|antw|fw|fwd|wg)\s*:\s*)+", "", mail.subject or "").strip()
        return self.db.latest_rfq_for(email, subj or None)

    def _book_one(self, q, team_ref: str, deadline: float, ex: Extraction) -> dict | None:
        try:
            return self.client.book(q["rate_id"], team_ref, deadline=deadline)
        except ApiError as exc:
            if exc.status not in (404, 410):
                ex.flags.append(f"booking {q['rate_id']} failed: {exc.status} {str(exc.detail)[:80]}")
                return None
        raw = json.loads(q["raw"] or "{}")  # rate expired: re-price same airline/weight, book new rate_id
        try:
            new = self.client.rate(q["origin"], q["destination"], raw.get("airline_code") or q["airline_code"],
                                   float(raw.get("weight_kg") or q["weight_kg"]), q["volume_cbm"] or 0,
                                   deadline=deadline)
            ex.flags.append(f"rate expired: re-priced {q['total']} -> {new.get('total')} {new.get('currency')}")
            return self.client.book(new["rate_id"], team_ref, deadline=deadline)
        except (ApiError, KeyError, TypeError, ValueError) as exc:
            ex.flags.append(f"re-booking failed: {exc}")
            return None

    def handle_booking(self, mail: Mail, ex: Extraction, deadline: float) -> str:
        rfq_id = self.find_rfq(mail)
        if rfq_id is None:
            ex.flags.append("booking: no matching quote found")
            return "unmatched"
        existing = self.db.bookings_for_rfq(rfq_id)
        booked: list[dict] = []
        if existing:
            ex.flags.append(f"booking for RFQ {rfq_id} already exists: re-confirming")
            booked = [dict(r) | {"pickup": json.loads(r["pickup"]) if r["pickup"] else None} for r in existing]
        else:
            seen_lots = set()
            for q in self.db.quotes_for(rfq_id):
                if q["lot"] in seen_lots:
                    continue
                seen_lots.add(q["lot"])
                team_ref = f"{self.team}-{rfq_id}-{q['lot']}"
                res = self._book_one(q, team_ref, deadline, ex)
                if not res:
                    continue
                raw = json.loads(q["raw"] or "{}")
                pickup = raw.get("pickup")
                price = raw.get("grand_total") or res.get("price") or q["total"]
                b = {
                    "rfq_id": rfq_id, "lot": q["lot"], "customer": address(mail.sender),
                    "booking_reference": res.get("booking_reference"), "team_reference": team_ref,
                    "rate_id": res.get("rate_id", q["rate_id"]),
                    "airline": res.get("airline") or q["airline"], "origin": q["origin"],
                    "destination": q["destination"], "price": str(price),
                    "currency": res.get("currency") or q["currency"], "status": res.get("status", "booked"),
                    "pickup": json.dumps(pickup) if pickup else None, "transit_days": q["transit_days"],
                    "raw": json.dumps(res, default=str),
                }
                self.db.save_booking(**b)
                booked.append(b | {"pickup": pickup})
                ex.flags.append(f"BOOKED {b['booking_reference']} ({b['airline']}, {b['price']} {b['currency']})")
        if not booked:
            return "booking_failed"
        if not self.reply:
            return "booked"
        draft = booking_draft(booked)
        body = self.writer.write("booking confirmation to a customer", draft,
                                 [b["booking_reference"] for b in booked])
        try:
            self.client.send_message(address(mail.sender), re_subject(mail.subject), body,
                                     in_reply_to=mail.id, deadline=max(deadline, time.monotonic() + 10))
            ex.flags.append("confirmation sent")
        except ApiError as exc:
            ex.flags.append(f"confirmation failed: {exc}")
        for b in booked:  # confirm pickup with the winning trucker
            p = b.get("pickup")
            if not p or existing:
                continue
            email = next((e for e, v in (p.get("bids") or {}).items() if str(v) == str(p["price_eur"])), None)
            if not email:
                continue
            tdraft = (f"Hello {p['trucker']},\n\nwe accept your offer of EUR {p['price_eur']} for the pickup "
                      f"{p['zip']} {p['city']} -> {p['airport']}. Booking reference: {b['booking_reference']}.\n\n"
                      f"Best regards,\n{self.writer.signature}")
            tbody = self.writer.write("pickup order confirmation to a trucker", tdraft,
                                      [p["price_eur"], p["zip"], b["booking_reference"]])
            try:
                self.client.send_message(email, f"Pickup order {p['zip']} {p['city']} to {p['airport']}", tbody,
                                         deadline=max(deadline, time.monotonic() + 10))
                ex.flags.append(f"pickup ordered from {p['trucker']}")
            except ApiError as exc:
                ex.flags.append(f"pickup order failed: {exc}")
        return "booked"

    def handle_tracking(self, mail: Mail, ex: Extraction, deadline: float) -> str:
        rows = self.db.find_bookings(address(mail.sender), f"{mail.subject}\n{mail.body}")
        ex.flags.append(f"tracking: {len(rows)} booking(s) found")
        if not self.reply:
            return "done"
        draft = tracking_draft(rows)
        body = self.writer.write("shipment status reply to a customer", draft,
                                 [r["booking_reference"] for r in rows] + [r["status"] for r in rows])
        try:
            self.client.send_message(address(mail.sender), re_subject(mail.subject), body,
                                     in_reply_to=mail.id, deadline=max(deadline, time.monotonic() + 10))
            ex.flags.append("status reply sent")
            return "replied"
        except ApiError as exc:
            ex.flags.append(f"status reply failed: {exc}")
            return "error"

    def check_pending(self) -> None:
        for req in self.pickup.due():
            bids = {e: p for e, p in req.bids.items() if p is not None}
            if bids:
                email = min(bids, key=bids.get)
                pickup = {"zip": req.info.zip, "city": req.info.city, "airport": req.info.airport,
                          "price_eur": str(bids[email]), "trucker": req.asked.get(email, email),
                          "bids": {k: str(v) for k, v in bids.items()}}
                for lot in req.lots:
                    lot.pickup = pickup
            else:
                req.ex.flags.append(f"no pickup bid received from {len(req.asked)} truckers: air only")
            self.finish(req.mail, req.ex, req.lots, req.deadline, req.window_end, req.t0)

    def _save(self, mail: Mail, ex: Extraction, status: str, flags: list[str] | None = None) -> None:
        self.db.save_message(
            mail.id,
            received_at=mail.received_at.isoformat() if mail.received_at else None,
            sender=mail.sender,
            subject=mail.subject,
            category=ex.category,
            method=ex.method,
            status=status,
            flags=flags if flags is not None else ex.flags,
            llm_tokens=ex.llm_tokens,
        )

    def process_safe(self, summary: dict) -> None:
        try:
            self.process(summary)
        except Exception as exc:  # noqa: BLE001  never let one mail kill the loop
            log.exception("message %s failed", summary.get("id"))
            try:
                self.db.save_message(
                    int(summary["id"]), received_at=None, sender="", subject=str(summary.get("subject", "")),
                    category="error", method="none", status="error", flags=[repr(exc)],
                )
            except Exception:  # noqa: BLE001
                pass

    # -- loop -------------------------------------------------------------------
    def skip_backlog(self) -> int:
        since = self.db.get_since_id()
        while True:
            data = self.client.poll(since, 200)
            msgs = messages_from_poll(data)
            if not msgs:
                break
            since = next_since_id(data, msgs, since)
            if len(msgs) < 200:
                break
        self.db.set_since_id(since)
        log.info("backlog skipped, since_id=%s", since)
        return since

    def run(self, once: bool = False) -> None:
        since = self.db.get_since_id()
        log.info("polling from since_id=%s every %.1fs", since, self.cfg.poll_interval_s)
        while True:
            try:
                data = self.client.poll(since, 100)
            except ApiError as exc:
                log.warning("poll failed: %s", exc)
                if once:
                    return
                time.sleep(self.cfg.poll_interval_s)
                continue
            msgs = sorted(messages_from_poll(data), key=lambda m: int(m.get("id", 0)))
            for m in msgs:  # sequential, oldest first
                mid = int(m["id"])
                if not self.db.is_processed(mid):
                    self.process_safe(m)
                since = max(since, mid)
                self.db.set_since_id(since)
            since = next_since_id(data, msgs, since)
            self.db.set_since_id(since)
            self.check_pending()
            if len(msgs) >= 100:
                continue  # more pending
            if once and not self.pickup.pending:
                return
            time.sleep(self.cfg.poll_interval_s)


# ---------------------------------------------------------------------------
# --dump: print raw API responses to learn the real shapes
# ---------------------------------------------------------------------------
def dump(client: FiveUClient, since_id: int, max_chars: int) -> None:
    def show(title: str, fn) -> Any:
        print(f"\n===== {title} =====")
        try:
            data = fn()
        except ApiError as exc:
            print(f"ERROR {exc}")
            return None
        text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        print(text[:max_chars] + (f"\n... ({len(text)} chars, truncated)" if len(text) > max_chars else ""))
        return data

    show("GET /v1/me", client.me)
    poll = show(f"GET /v1/mailbox/messages?since_id={since_id}&limit=5", lambda: client.poll(since_id, 5))
    msgs = messages_from_poll(poll)
    if msgs and "id" in msgs[0]:
        mid = int(msgs[0]["id"])
        show(f"GET /v1/mailbox/messages/{mid}", lambda: client.message(mid))
    ports = show("GET /v1/portal/ports", client.ports)
    lanes = ports.get("lanes", []) if isinstance(ports, dict) else []
    if lanes and lanes[0].get("airlines"):
        lane, airline = lanes[0], lanes[0]["airlines"][0]
        code = airline.get("code") if isinstance(airline, dict) else airline
        show(f"GET /v1/portal/brackets?airline={code}", lambda: client.brackets(code))
        show(
            f"GET /v1/portal/rates {lane['origin']}-{lane['destination']} {code} 100 kg 0.5 cbm",
            lambda: client.rate(lane["origin"], lane["destination"], code, 100, 0.5),
        )
    show("GET /v1/gateway/models", client.models)
    print(f"\nrate limit: {client.rate_limit_remaining}/{client.rate_limit}")


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="quote_bot", description="5U air-freight RFQ quote bot (print only)")
    p.add_argument("--dump", action="store_true", help="print raw API responses and exit")
    p.add_argument("--dump-chars", type=int, default=4000, help="truncate each dumped response")
    p.add_argument("--once", action="store_true", help="process pending mails once, then exit")
    p.add_argument("--message-id", type=int, help="process one message by id (ignores processed state)")
    p.add_argument("--since-id", type=int, help="override stored since_id")
    p.add_argument("--skip-backlog", action="store_true", help="on start, jump past existing mails")
    p.add_argument("--no-llm", action="store_true", help="rules only, no gateway calls")
    p.add_argument("--reply", action="store_true", help="send the quote back to the customer")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "httpx", "httpx2", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = load_config()
    client = FiveUClient(cfg.base_url, cfg.api_key, cfg.http_timeout_s)

    if args.dump:
        dump(client, args.since_id or 0, args.dump_chars)
        return

    db = DB(cfg.db_path)
    if args.since_id is not None:
        db.set_since_id(args.since_id)

    auto = os.getenv("AUTO_REPLY", "").strip().lower() in ("1", "true", "yes")
    bot = Bot(cfg, client, db, use_llm=not args.no_llm, reply=args.reply or auto)
    bot.start()

    if args.message_id is not None:
        data = client.message(args.message_id)
        bot.process(data.get("message", data) if isinstance(data, dict) and "id" not in data else data)
        return
    if args.skip_backlog:
        bot.skip_backlog()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        bot.run(once=args.once)
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
