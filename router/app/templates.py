"""Plain fallback texts for the seven mail kinds.

Only used when the LLM-written mail fails the fact check twice (ADR 13), so that a mail
always goes out. Every template prints the values in `facts["must_appear"]` verbatim.
"""

from typing import Any

from app.schemas import ComposedMail


def _quote(f: dict[str, Any]) -> tuple[str, str]:
    pickup = f"Pickup from {f['pickup_from']} is included.\n" if f.get("pickup_included") else ""
    return (
        f"Your air freight quote {f['route']}",
        f"thank you for your request. Our offer for {f['route']} with {f['airline']} "
        f"(transit time {f['transit_days']} days):\n\n"
        f"All-in price: {f['all_in_price']}\n\n"
        f"{pickup}"
        f"Estimated departure: {f['estimated_departure']}\n"
        f"Estimated arrival: {f['estimated_arrival']}\n"
        f"Valid until: {f['valid_until']}\n\n"
        "Simply reply to this email and we will book it.",
    )


def _trucker_request(f: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Pickup quote {f['pickup_postal_code']} {f.get('pickup_city') or ''} to {f['airport']} "
        f"on {f['ready_date']}".replace("  ", " "),
        "please quote the following pickup:\n"
        f"From: {f['pickup_postal_code']} {f.get('pickup_city') or ''}\n"
        f"To: {f['airport']} airport (air export warehouse)\n"
        f"Ready date: {f['ready_date']}\n"
        f"Pieces: {f['pieces']}\n"
        f"Gross weight: {f['gross_weight']}\n"
        f"Volume: {f['volume']}\n\n"
        "Please reply with your all-in price in EUR, the pickup time and the validity.",
    )


def _airline_rate_request(f: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Rate request {f['lane']} on {f['ready_date']}, our reference {f['our_reference']}",
        f"please quote air freight on lane {f['lane']}:\n"
        f"Ready date: {f['ready_date']}\n"
        f"Pieces: {f['pieces']}\n"
        f"Gross weight: {f['gross_weight']}\n"
        f"Volume: {f['volume']}\n"
        f"Commodity: {f.get('commodity') or 'general cargo'}\n"
        f"Our reference: {f['our_reference']}\n\n"
        "Please reply with your all-in price in EUR airport to airport (freight, fuel and "
        "security), the transit time in days and the validity.",
    )


def _no_price(f: dict[str, Any]) -> tuple[str, str]:
    return (
        "Your air freight request",
        f"thank you for your request to {f['destination']}. Unfortunately we could not get an "
        "airline price for this route in time, so we cannot send you a quote for it right now.",
    )


def _clarification(f: dict[str, Any]) -> tuple[str, str]:
    questions = "\n".join(f"- {item}" for item in f["missing_fields"])
    return (
        "Your air freight request",
        "thank you for your request. We are preparing your quote and need the following "
        f"to price it correctly:\n{questions}",
    )


def _booking_note(f: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Booking confirmed {f['route']}",
        f"thank you, your shipment {f['route']} is booked with {f['airline']}.\n"
        f"Booking reference: {f['booking_ref']}\n"
        f"Estimated arrival: {f['estimated_arrival']}\n\n"
        f"The goods move on the ready date, {f['ready_date']}.",
    )


def _booking_request(f: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Booking request {f['lane']}, our reference {f['our_reference']}",
        f"please book the following shipment on lane {f['lane']}:\n"
        f"Ready date: {f['ready_date']}\n"
        f"Pieces: {f['pieces']}\n"
        f"Gross weight: {f['gross_weight']}\n"
        f"Volume: {f['volume']}\n"
        f"Chargeable weight: {f['chargeable_weight']}\n"
        f"Rate per your tariff: {f['tariff_total']}\n"
        f"Our reference: {f['our_reference']}\n"
        + (f"Your offer reference: {f['your_offer_reference']}\n" if f.get("your_offer_reference") else "")
        + "\nPlease confirm the booking with your booking reference.",
    )


def _answer(f: dict[str, Any]) -> tuple[str, str]:
    return ("Your question", "thank you for your message. We will confirm the details shortly.")


def _tracking_answer(f: dict[str, Any]) -> tuple[str, str]:
    if not f.get("found"):
        return (
            "Your shipment status",
            "thank you for your message. We could not match your shipment from the details "
            "given. Could you please send us your booking reference?",
        )
    return (
        "Your shipment status",
        f"here is the current status of your shipment {f['route']} with {f['airline']}:\n\n"
        f"Status: {f['status']}\n"
        f"Booking reference: {f['booking_reference']}\n"
        f"Planned transit time: {f['transit_days']} days\n"
        f"Estimated arrival: {f['estimated_arrival']}",
    )


_TEMPLATES = {
    "quote": _quote,
    "trucker_request": _trucker_request,
    "airline_rate_request": _airline_rate_request,
    "no_price": _no_price,
    "clarification": _clarification,
    "booking_note": _booking_note,
    "booking_request": _booking_request,
    "answer": _answer,
    "tracking_answer": _tracking_answer,
}
MAIL_KINDS = tuple(_TEMPLATES)


def render(kind: str, facts: dict[str, Any], signature: str, recipient_name: str = "") -> ComposedMail:
    """The template mail for `kind`."""
    subject, text = _TEMPLATES[kind](facts)
    greeting = f"Dear {recipient_name}," if recipient_name else "Hello,"
    return ComposedMail(subject=subject, body=f"{greeting}\n\n{text}\n\nBest regards,\n{signature}")
