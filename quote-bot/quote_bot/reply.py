"""Build and send the quote reply mail (POST /v1/mailbox/messages, in_reply_to)."""

from __future__ import annotations

import os
import re

from .api import FiveUClient
from .extract import Mail
from .pricing import LotQuote, grand_total

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def address(sender: str) -> str:
    m = EMAIL_RE.search(sender or "")
    return m.group(0) if m else (sender or "").strip()


def _g(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def build_body(lots: list[LotQuote]) -> str:
    sig = os.getenv("REPLY_SIGNATURE", "Air Freight Desk").strip()
    lines = ["Dear customer,", "", "thank you for your request. Our offer:", ""]
    for i, lot in enumerate(lots, 1):
        s, b = lot.shipment, lot.best
        r = b.rate
        total, cur, pickup_conv = grand_total(lot)
        head = f"Shipment {i}: " if len(lots) > 1 else ""
        lines += [
            f"{head}{s.origin} -> {s.destination}",
            f"  Weight: {_g(s.weight_kg)} kg | Volume: {_g(s.volume_cbm)} cbm"
            f" | Chargeable: {_g(r.get('chargeable_weight_kg'))} kg",
            f"  Carrier: {b.airline.label}",
            *(
                [f"  Air freight: {b.total} {cur}",
                 f"  Pickup {lot.pickup['zip']} {lot.pickup['city']} -> {lot.pickup['airport']}: {pickup_conv} {cur}"]
                if pickup_conv is not None else []
            ),
            f"  Total price: {total} {cur} (all-in)",
            f"  Transit time: {r.get('transit_days')} days",
            f"  Valid until: {r.get('valid_until')}",
            f"  Quote reference: {r.get('rate_id')}",
            "",
        ]
    lines += ["Reply to this mail to confirm the booking.", "", "Best regards,", sig]
    return "\n".join(lines)


def re_subject(subject: str) -> str:
    return subject if re.match(r"(?i)^\s*re:", subject or "") else f"Re: {subject}"


def send_quote(client: FiveUClient, mail: Mail, lots: list[LotQuote], deadline: float,
               writer=None) -> dict | None:
    """Send only if every lot has a price (only the first reply scores)."""
    if not lots or not all(lot.best for lot in lots):
        return None
    draft = build_body(lots)
    body = draft
    if writer is not None:
        must = []
        for lot in lots:
            total, cur, _ = grand_total(lot)
            must += [str(total), cur, lot.best.rate.get("rate_id")]
        body = writer.write("price quotation reply to a customer", draft, must)
    return client.send_message(
        to=address(mail.sender), subject=re_subject(mail.subject), body=body,
        in_reply_to=mail.id, deadline=deadline,
    )


def booking_draft(bookings: list[dict]) -> str:
    sig = os.getenv("REPLY_SIGNATURE", "Air Freight Desk").strip()
    lines = ["Dear customer,", "", "thank you for your order. Your booking is confirmed:", ""]
    for b in bookings:
        lines += [
            f"{b['origin']} -> {b['destination']} with {b['airline']}",
            f"  Booking reference: {b['booking_reference']}",
            f"  Status: {b['status']}",
            f"  Price: {b['price']} {b['currency']}",
        ]
        if b.get("pickup"):
            p = b["pickup"]
            lines.append(f"  Pickup: {p['zip']} {p['city']} -> {p['airport']} by {p['trucker']}")
        lines.append("")
    lines += ["We will keep you updated.", "", "Best regards,", sig]
    return "\n".join(lines)


def tracking_draft(rows: list) -> str:
    sig = os.getenv("REPLY_SIGNATURE", "Air Freight Desk").strip()
    if not rows:
        return "\n".join(["Dear customer,", "",
                          "we could not find a booking under your address. Could you send us your "
                          "booking reference?", "", "Best regards,", sig])
    lines = ["Dear customer,", "", "here is the current status of your shipment:", ""]
    for r in rows:
        lines += [
            f"{r['origin']} -> {r['destination']} with {r['airline']}",
            f"  Booking reference: {r['booking_reference']}",
            f"  Status: {r['status']}",
            f"  Booked: {r['created_at']}",
        ]
        if r["transit_days"] is not None:
            lines.append(f"  Planned transit time: {r['transit_days']} days")
        lines.append("")
    lines += ["Best regards,", sig]
    return "\n".join(lines)
