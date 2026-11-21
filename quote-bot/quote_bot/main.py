"""Entry point: python -m quote_bot [--dump | --once | --message-id N] [--no-llm] [--skip-backlog]"""

from __future__ import annotations

import argparse
import hashlib
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
    BOOKING_CONFIRMED_RE,
    Extraction,
    Shipment,
    load_airline_emails,
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
from .reply import address, booking_draft, booking_ref, re_subject, send_quote, tracking_draft
from .attachments import read_attachments
from .dg import dg_evidence, dg_request_body, has_dg_documents
from .tariffs import LLM_TARIFF_PROMPT, TariffStore, parse_llm_tariff, parse_tariff
from .extract import find_volume, find_weight, volume_from_dims
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
        self.tariffs = TariffStore(db.conn, db.lock)
        use_tariffs = os.getenv("TARIFF_PRICING", "1").strip().lower() not in ("0", "false", "no")
        self.pricer = Pricer(client, self.lanes, BracketCache(client, cfg.brackets_ttl_s), cfg.rate_workers,
                             tariffs=self.tariffs if use_tariffs else None)
        self._reply_override: dict[int, Mail] = {}  # RFQ id -> mail to answer (DG documents reply)
        self._skip_dg: set[int] = set()
        self.llm: LLMExtractor | None = None
        self.use_llm = use_llm
        self.own: set[str] = set()
        self.reply = reply
        self.writer = Writer(None)
        self.team = "team"
        carriers_path = os.getenv("CARRIER_LIST", "start-kit/carrier-list.csv")
        self.carriers = load_carriers(carriers_path)
        self.airline_emails = load_airline_emails(carriers_path)
        self.email_to_airline = {e: n for n, e in self.airline_emails.items()}
        seed = os.getenv("TARIFF_SEED_CSV", "data/261121_tariffs_normalized_01.csv")
        if use_tariffs and seed:
            names = {}
            try:
                import csv as _csv
                with open(carriers_path, encoding="utf-8") as fh:
                    names = {r["name"].strip().lower(): r["email"].strip().lower() for r in _csv.DictReader(fh)}
            except OSError:
                pass
            n = self.tariffs.seed_from_csv(seed, names, "2026-11-01", "2026-11-30")
            log.info("tariffs: %d lanes stored (%d new from %s), pricing %s", self.tariffs.count(), n, seed,
                     "on" if use_tariffs else "off")
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

        dg_rfq = self._awaiting_dg(mail)
        if dg_rfq is not None:  # customer answers our DG document request -> quote the original RFQ now
            ex0 = Extraction("dg_docs", method="rules", flags=[f"DG documents for RFQ {dg_rfq}: quoting now"])
            print_non_rfq(mail, ex0)
            self._save(mail, ex0, "done")
            self._reply_override[dg_rfq] = mail
            self._skip_dg.add(dg_rfq)
            self.process(self.client.message(dg_rfq))
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

        if ex.category == "rfq" and mail.is_reply and self._thread_quoted(mail):
            ex.flags.append("reply in a thread we already quoted: no second quote (only the first one counts)")
            ex.category = "other"

        if ex.category == "rfq" and ex.method == "llm" and any(
            s.origin and s.origin == s.destination for s in ex.shipments
        ):
            ex.flags.append("origin == destination: not a valid air RFQ")
            ex.category = "other"

        if ex.category == "rate_sheet":
            status = self.handle_rate_sheet(mail, ex)
            print_non_rfq(mail, ex)
            self._save(mail, ex, status)
            return

        if ex.category == "booking_confirmed":
            status = self.handle_booking_confirmed(mail, ex)
            print_non_rfq(mail, ex)
            self._save(mail, ex, status)
            return

        if ex.category == "carrier_quote":
            self.handle_bid(mail, ex)
            print_non_rfq(mail, ex)
            self._save(mail, ex, "done")
            self.check_pending()
            return

        if ex.category == "declined":
            ex.flags.append("customer declined: not booked")
            print(f"✗ DECLINED by {address(mail.sender)} (MSG {mail.id}): not booked", flush=True)
            print_non_rfq(mail, ex)
            self._save(mail, ex, "declined")
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

        # attachments (packing list, specs): dangerous goods check + missing weight/volume
        atts = read_attachments(self.client, mail, self.llm) if mail.attachments else []
        for a in atts:
            ex.llm_tokens += a.llm_tokens
            if a.note:
                ex.flags.append(f"attachment {a.filename}: {a.note}")
        att_text = "\n".join(a.text for a in atts if a.text)
        if att_text:
            for s in ex.shipments:
                if not s.weight_kg:
                    w = find_weight(att_text).value
                    if w:
                        s.weight_kg = w
                        s.flags.append(f"weight {w} kg from attachment")
                if s.volume_cbm is None:
                    v = find_volume(att_text).value or volume_from_dims(att_text).value
                    if v:
                        s.volume_cbm = v
                        s.flags.append(f"volume {v} cbm from attachment")
        if mail.id not in self._skip_dg:
            evidence = dg_evidence(f"{text}\n{att_text}")
            if evidence and not has_dg_documents([text, att_text], [a.filename for a in atts]):
                return self._request_dg_docs(mail, ex, evidence, deadline)
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
                target = self._reply_override.pop(mail.id, None) or mail
                sent = send_quote(self.client, target, lots, max(deadline, time.monotonic() + 10), self.writer)
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

    def _airline_email(self, airline: str, detail: Any = "", explicit: str | None = None) -> str | None:
        """Address to book by e-mail: tariff sender, one named in the 503 message, else the carrier list."""
        if explicit:
            return explicit.lower()
        for m in EMAIL_ANY_RE.findall(str(detail or "")):
            m = m.lower()
            if m in self.carriers or m.endswith("@hackloop.mail"):
                return m
        name = (airline or "").strip().lower()
        if name in self.airline_emails:
            return self.airline_emails[name]
        for n, e in self.airline_emails.items():  # "Xenon Cargo Airlines (XC)" etc.
            if n and (n in name or name in n):
                return e
        return None

    def _book_by_email(self, q, rate: dict, team_ref: str, detail: Any, deadline: float,
                       ex: Extraction) -> dict | None:
        airline = rate.get("airline") or q["airline"]
        to = self._airline_email(airline, detail, rate.get("carrier_email"))
        ex.flags.append(f"booking by e-mail ({str(detail)[:120]})")
        if not to:
            ex.flags.append(f"no e-mail for airline {airline}: cannot book by e-mail")
            return None
        body = (
            f"Hello {airline},\n\nplease book the following shipment on your rate:\n\n"
            f"Rate ID: {rate.get('rate_id')}\n"
            f"Our reference: {team_ref}\n"
            f"Route: {rate.get('origin') or q['origin']} -> {rate.get('destination') or q['destination']}\n"
            f"Weight: {rate.get('weight_kg') or q['weight_kg']} kg | Volume: {q['volume_cbm'] or 0} cbm"
            f" | Chargeable: {rate.get('chargeable_weight_kg') or q['chargeable_weight_kg']} kg"
            + (f" | Pieces: {q['pieces']}" if q["pieces"] else "") + "\n"
            f"Price: {rate.get('total') or q['total']} {rate.get('currency') or q['currency']}\n\n"
            f"Please confirm with your booking reference.\n\nBest regards,\n{self.writer.signature}"
        )
        try:
            self.client.send_message(to, f"Booking request {rate.get('rate_id')} / {team_ref}", body,
                                     deadline=max(deadline, time.monotonic() + 10))
        except ApiError as exc:
            ex.flags.append(f"booking e-mail to {to} failed: {exc}")
            return None
        ex.flags.append(f"booking requested by e-mail from {to}")
        return {"booking_reference": None, "team_reference": team_ref, "rate_id": rate.get("rate_id"),
                "airline": airline, "price": rate.get("total"), "currency": rate.get("currency"),
                "status": "requested"}

    def _book_one(self, q, team_ref: str, deadline: float, ex: Extraction) -> dict | None:
        """Book the quoted rate; expired/unknown -> fresh rate (same airline, else cheapest)."""
        raw = json.loads(q["raw"] or "{}")
        rate = {**raw, "rate_id": q["rate_id"]}
        if raw.get("source") == "tariff":  # rate-sheet airline: no portal rate_id, book with the airline
            return self._book_by_email(q, rate, team_ref, "rate-sheet airline", deadline, ex)
        try:
            return self.client.book(q["rate_id"], team_ref, deadline=max(deadline, time.monotonic() + 15))
        except ApiError as exc:
            if exc.status == 503:
                return self._book_by_email(q, rate, team_ref, exc.detail, deadline, ex)
            if exc.status == 409:  # this rate_id is already booked under another reference
                ex.flags.append(f"rate {q['rate_id']} already booked (409): {str(exc.detail)[:80]}")
                return {"booking_reference": None, "team_reference": team_ref, "rate_id": q["rate_id"],
                        "airline": q["airline"], "price": q["total"], "currency": q["currency"],
                        "status": "booked"}
            if exc.status not in (404, 410):
                ex.flags.append(f"booking {q['rate_id']} failed: {exc.status} {str(exc.detail)[:80]}")
                return None
            ex.flags.append(f"rate {q['rate_id']} {exc.status}: fetching a fresh rate")
        weight = float(raw.get("weight_kg") or q["quoted_weight_kg"] or q["weight_kg"])
        new = None
        try:  # same airline, same billed weight
            new = self.client.rate(q["origin"], q["destination"], raw.get("airline_code") or q["airline_code"],
                                   weight, q["volume_cbm"] or 0, deadline=max(deadline, time.monotonic() + 15))
        except ApiError as exc:
            ex.flags.append(f"re-price same airline failed: {exc.status}")
        if not new or not new.get("rate_id"):
            try:  # airline gone (hourly change): cheapest airline on the lane
                lot = self.pricer.quote(Shipment(q["origin"], q["destination"], float(q["weight_kg"]),
                                                 q["volume_cbm"] or 0.0, q["pieces"]),
                                        max(deadline, time.monotonic() + 20))
                new = dict(lot.best.rate) if lot.best else None
            except Exception as exc:  # noqa: BLE001
                ex.flags.append(f"re-quote failed: {exc}")
        if not new or not new.get("rate_id"):
            ex.flags.append("no fresh rate: booking failed")
            return None
        ex.flags.append(f"re-priced {q['total']} -> {new.get('total')} {new.get('currency')} ({new.get('airline')})")
        try:
            return self.client.book(new["rate_id"], team_ref, deadline=max(deadline, time.monotonic() + 15))
        except ApiError as exc:
            if exc.status == 503:
                return self._book_by_email(q, new, team_ref, exc.detail, deadline, ex)
            ex.flags.append(f"re-booking failed: {exc.status} {str(exc.detail)[:80]}")
            return None

    def handle_booking(self, mail: Mail, ex: Extraction, deadline: float) -> str:
        rfq_id = self.find_rfq(mail)
        if rfq_id is None:
            ex.flags.append("booking: no matching quote found")
            print(f"✗ BOOKING UNMATCHED MSG {mail.id}: no quote with a rate_id for {mail.sender!r}", flush=True)
            return "unmatched"
        existing = self.db.bookings_for_rfq(rfq_id)
        booked: list[dict] = []
        if existing:  # already booked + confirmed: no second confirmation (customer thank-you loop)
            ex.flags.append(f"RFQ {rfq_id} already booked ({', '.join(booking_ref(r) for r in existing)}): no new mail")
            return "duplicate"
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
                    "booking_reference": res.get("booking_reference"), "team_reference": res.get("team_reference") or team_ref,
                    "rate_id": res.get("rate_id") or q["rate_id"],
                    "airline": res.get("airline") or q["airline"], "origin": q["origin"],
                    "destination": q["destination"], "price": str(price),
                    "currency": res.get("currency") or q["currency"], "status": res.get("status", "booked"),
                    "pickup": json.dumps(pickup) if pickup else None, "transit_days": q["transit_days"],
                    "raw": json.dumps(res, default=str),
                }
                self.db.save_booking(**b)
                booked.append(b | {"pickup": pickup})
                ex.flags.append(f"BOOKED {booking_ref(b)} [{b['status']}] ({b['airline']}, {b['price']} {b['currency']})")
                print(f"✓ BOOKED RFQ {rfq_id} lot {q['lot']}: {booking_ref(b)} [{b['status']}]", flush=True)
        if not booked:
            print(f"✗ BOOKING FAILED for MSG {mail.id} (RFQ {rfq_id}): {'; '.join(ex.flags)}", flush=True)
            return "booking_failed"
        if not self.reply:
            return "booked"
        draft = booking_draft(booked)
        body = self.writer.write("booking confirmation to a customer", draft,
                                 [booking_ref(b) for b in booked])
        try:
            self.client.send_message(address(mail.sender), re_subject(mail.subject), body,
                                     in_reply_to=mail.id, deadline=max(deadline, time.monotonic() + 10))
            ex.flags.append("confirmation sent")
        except ApiError as exc:
            ex.flags.append(f"confirmation failed: {exc}")
        # no pickup order mail: truckers state "no booking needed, we collect once the air booking is confirmed"
        return "booked"

    def _thread_quoted(self, mail: Mail) -> bool:
        for r in ref_ids(mail):
            for cand in (r, self.db.sent_parent(r)):
                if cand is not None and self.db.quotes_for(cand):
                    return True
        return False

    def _awaiting_dg(self, mail: Mail) -> int | None:
        """RFQ id if this mail answers our dangerous-goods document request in that thread."""
        if not mail.is_reply:
            return None
        sender = address(mail.sender).lower()
        for r in ref_ids(mail):
            for cand in (r, self.db.sent_parent(r)):
                row = self.db.message(cand) if cand is not None else None
                if row is not None and row["status"] == "awaiting_dg_docs" and \
                        address(row["sender"] or "").lower() == sender:
                    return int(row["id"])
        return None

    def _request_dg_docs(self, mail: Mail, ex: Extraction, evidence: list[str], deadline: float) -> None:
        ex.flags.append("dangerous goods: " + "; ".join(evidence[:4]))
        status = "awaiting_dg_docs"
        if self.reply:
            body = dg_request_body(evidence, self.writer.signature)  # template, no LLM
            try:
                self.client.send_message(address(mail.sender), re_subject(mail.subject), body, in_reply_to=mail.id,
                                         deadline=max(deadline, time.monotonic() + 10))
                ex.flags.append("DG documents requested (no quote yet)")
            except ApiError as exc:
                ex.flags.append(f"DG request failed: {exc}")
        print(f"⚠ DANGEROUS GOODS MSG {mail.id}: {'; '.join(evidence[:4])} -> documents requested", flush=True)
        print_non_rfq(mail, ex)
        self._save(mail, ex, status)

    def handle_rate_sheet(self, mail: Mail, ex: Extraction) -> str:
        """Tariff ingestion: body + attachments -> tariffs table (used by the pricer)."""
        m = EMAIL_ANY_RE.search(mail.sender or "")
        sender = m.group(0).lower() if m else ""
        list_name = self.email_to_airline.get(sender)
        hint = None
        if list_name:  # use the carrier-list spelling of the airline name
            for n in self._carrier_names():
                if n.lower() == list_name:
                    hint = n
        sources = [("mail body", strip_quoted(mail.body) or mail.body or "")]
        for a in read_attachments(self.client, mail, self.llm):
            ex.llm_tokens += a.llm_tokens
            if a.note:
                ex.flags.append(f"{a.filename}: {a.note}")
            sources.append((a.filename, a.text))
        stored = []
        for name, text in sources:
            if not text or not text.strip():
                continue
            res = parse_tariff(text, hint)
            need_llm = not res.lanes and self.llm is not None and (
                (name != "mail body" and len(text) > 150)
                or (name == "mail body" and len(re.findall(r"\d+[.,]\d{2}", text)) >= 8))
            if need_llm:
                try:
                    content, tok = self.llm.extract_tariff(text, LLM_TARIFF_PROMPT)
                    ex.llm_tokens += tok
                    res = parse_llm_tariff(content, hint)
                    ex.flags.append(f"{name}: parsed by LLM ({tok} tokens)")
                except Exception as exc:  # noqa: BLE001
                    ex.flags.append(f"{name}: LLM tariff parse failed: {exc}")
            for ln in res.lanes:
                ln.carrier_email = sender if list_name or not ln.carrier_email else ln.carrier_email
                ln.carrier_email = ln.carrier_email or self.airline_emails.get(ln.carrier.lower())
                ln.source = f"mail {mail.id}: {name}"
            if res.lanes:
                ex.flags.append(f"{name}: {len(res.lanes)} lanes ({res.carrier}, valid {res.valid_from}..{res.valid_to})")
            ex.flags += [f"{name}: {n}" for n in res.notes[:3]]
            stored += res.lanes
        if not stored:
            ex.flags.append("rate sheet: no lanes found")
            return "rate_sheet_empty"
        self.tariffs.save(stored)
        print(f"✓ TARIFF {len(stored)} lanes from {stored[0].carrier} stored (MSG {mail.id})", flush=True)
        return "ingested"

    def _carrier_names(self) -> list[str]:
        try:
            import csv as _csv
            with open(os.getenv("CARRIER_LIST", "start-kit/carrier-list.csv"), encoding="utf-8") as fh:
                return [r["name"].strip() for r in _csv.DictReader(fh)]
        except OSError:
            return []

    def handle_booking_confirmed(self, mail: Mail, ex: Extraction) -> str:
        """Airline mail "Booking BK-... confirmed": mark the booking confirmed in the CRM."""
        text = f"{mail.subject}\n{mail.body}"
        m = BOOKING_CONFIRMED_RE.search(text)
        ref = m.group(1).upper() if m else None
        row = self.db.booking_matching(text, ref)
        if row is not None:
            self.db.update_booking(row["id"], status="confirmed",
                                   booking_reference=row["booking_reference"] or ref)
            ex.flags.append(f"booking {ref} confirmed (RFQ {row['rfq_id']})")
            return "confirmed"
        for rid in re.findall(r"\bR-[A-Za-z0-9_-]{6,}", text):  # booked elsewhere: rebuild from the quote
            q = self.db.quote_by_rate_id(rid)
            if q is None:
                continue
            raw = json.loads(q["raw"] or "{}")
            msg = self.db.message(q["message_id"])
            tm = re.search(rf"\b{re.escape(self.team)}-\d+-\d+\b", text)
            self.db.save_booking(
                rfq_id=q["message_id"], lot=q["lot"], customer=address(msg["sender"] if msg else ""),
                booking_reference=ref, team_reference=tm.group(0) if tm else f"{self.team}-{q['message_id']}-{q['lot']}",
                rate_id=rid, airline=q["airline"], origin=q["origin"], destination=q["destination"],
                price=str(raw.get("grand_total") or q["total"]), currency=q["currency"], status="confirmed",
                pickup=json.dumps(raw["pickup"]) if raw.get("pickup") else None, transit_days=q["transit_days"],
                raw=json.dumps({"confirmation_mail": mail.id}),
            )
            ex.flags.append(f"booking {ref} confirmed for rate {rid} (RFQ {q['message_id']}), added to CRM")
            return "confirmed"
        ex.flags.append(f"confirmation {ref} matches no booking in the CRM")
        return "unmatched"

    def handle_tracking(self, mail: Mail, ex: Extraction, deadline: float) -> str:
        text = f"{mail.subject}\n{mail.body}"
        rows = self.db.find_bookings(address(mail.sender), text)
        quotes = []
        if not rows:
            rfq_id = self.find_rfq(mail)
            quotes = [q for q in self.db.quotes_for(rfq_id)] if rfq_id else []
            seen, uniq = set(), []
            for q in quotes:
                if q["lot"] not in seen:
                    seen.add(q["lot"])
                    uniq.append(q)
            quotes = uniq
        ex.flags.append(f"tracking: {len(rows)} booking(s), {len(quotes)} open quote(s) found")
        if not self.reply:
            return "done"
        draft = tracking_draft(rows, quotes)
        must = [booking_ref(r) for r in rows] or [q["rate_id"] for q in quotes]
        body = self.writer.write("shipment status reply to a customer", draft, must)
        try:
            self.client.send_message(address(mail.sender), re_subject(mail.subject), body,
                                     in_reply_to=mail.id, deadline=max(deadline, time.monotonic() + 10))
            ex.flags.append("status reply sent")
            print(f"→ TRACKING REPLY to {address(mail.sender)}: {len(rows)} booking(s)", flush=True)
            return "replied"
        except ApiError as exc:
            ex.flags.append(f"status reply failed: {exc}")
            return "error"

    def redo_bookings(self) -> None:
        """Re-run booking/tracking mails an older build skipped (confirmations first)."""
        rows = self.db.redo_candidates()
        rows = sorted(rows, key=lambda r: (0 if "confirmed" in (r["subject"] or "") else
                                           1 if r["category"] == "booking" else 2, r["id"]))
        log.info("redo: %d mail(s): %s", len(rows), [r["id"] for r in rows])
        for r in rows:
            try:
                self.process(self.client.message(int(r["id"])))
            except Exception:  # noqa: BLE001
                log.exception("redo %s failed", r["id"])

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
def build_id() -> str:
    """Short hash of the package source: shows which code a running bot uses."""
    from pathlib import Path

    h = hashlib.sha1()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.read_bytes())
    return h.hexdigest()[:8]


def single_instance(db_path: str):
    """Refuse to start a second polling bot on the same DB (double replies, stale code)."""
    try:
        import fcntl
    except ImportError:  # Windows
        return None
    fh = open(db_path + ".lock", "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        other = fh.read().strip()
        raise SystemExit(f"another quote_bot loop already runs on {db_path} ({other}). "
                         f"Stop it first:  pkill -f quote_bot")
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid {os.getpid()} build {build_id()}")
    fh.flush()
    return fh


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
    p.add_argument("--redo-bookings", action="store_true",
                   help="on start, re-run booking/tracking mails an older build did not handle")
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

    log.info("quote_bot build %s | db %s", build_id(), os.path.abspath(cfg.db_path))
    loop_mode = args.message_id is None
    if loop_mode:
        lock = single_instance(str(cfg.db_path))  # noqa: F841  (held until exit)
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
    if args.redo_bookings:
        bot.redo_bookings()
    if args.skip_backlog:
        bot.skip_backlog()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        bot.run(once=args.once)
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
