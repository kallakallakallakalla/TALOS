"""What an operator can do from the CRM, each through an operation the workflow already has.

Every action except `dismiss` and `restore` changes a shipment or sends mail, so it is only
allowed in the process that runs the mailbox poller: that process owns the state machine.
A second process looking at the same database (the read-only viewer) must not book or send
behind its back. Actions take the poller's work lock, so they never run in between two
steps of a poll cycle. Whether mail really leaves is still decided by SEND_ENABLED.
"""

from typing import Any

from app.config import Settings
from app.extractors import missing_fields
from app.poller import MailboxPoller
from app.schemas import ShipmentState
from app.storage import MessageStore

S = ShipmentState
CLOSED_BY_OPERATOR = "closed by operator"
GAVE_UP = "processing failed"  # start of the reasoning the poller stores when it gives up on a mail
ANYWHERE = {"dismiss", "restore"}  # only touch the CRM's own table


class ActionError(Exception):
    """The action does not apply (wrong state, nothing to act on, not allowed here)."""


def may_act(poller: MailboxPoller | None) -> bool:
    return bool(poller and poller.running and poller.workflow)


def _settings(poller: MailboxPoller) -> Settings:
    return poller._settings  # noqa: SLF001 - the limits the workflow itself applies


async def run(name: str, *, store: MessageStore, poller: MailboxPoller | None, params: dict[str, Any]) -> str:
    """Carry out one action and return a sentence saying what happened."""
    if name == "dismiss":
        if not params.get("key"):
            raise ActionError("No item named.")
        store.dismiss(str(params["key"]))
        return "Item dismissed."
    if name == "restore":
        return f"{store.restore_dismissed()} dismissed items restored."
    if name not in _ACTIONS:
        raise ActionError(f"Unknown action: {name}")
    if not may_act(poller):
        raise ActionError(
            "This page is a read-only view: the router that processes the mailbox is not running in this "
            "process. Open the CRM on the router's own address to act."
        )
    async with poller.work_lock:
        return await _ACTIONS[name](store, poller, params)


def _number(params: dict[str, Any], name: str) -> int:
    try:
        return int(params.get(name) or 0)
    except (TypeError, ValueError):
        raise ActionError(f"{name} is not a number.") from None


def _shipment(store: MessageStore, params: dict[str, Any]):
    shipment = store.get_shipment(_number(params, "shipment_id"))
    if shipment is None:
        raise ActionError("Shipment not found.")
    return shipment


async def _retry_booking(store: MessageStore, poller: MailboxPoller, params: dict[str, Any]) -> str:
    shipment = _shipment(store, params)
    gave_up = shipment.state == S.FAILED and (shipment.error or "").startswith("not booked")
    if shipment.state != S.BOOKING and not gave_up:
        raise ActionError(f"Shipment {shipment.id} is {shipment.state.value}; there is no open booking to retry.")
    if shipment.chosen_option is None:
        raise ActionError("No flight is stored for this shipment.")
    if gave_up:  # start a fresh round of attempts
        shipment.state, shipment.booking_attempts, shipment.error = S.BOOKING, 0, None
        store.save_shipment(shipment)
    await poller.workflow._book(shipment)  # noqa: SLF001 - the workflow's own booking step
    after = store.get_shipment(shipment.id)
    return f"Booking attempted; shipment {after.id} is now {after.state.value}" + (f" ({after.booking_ref})." if after.booking_ref else ".")


async def _remind_customer(store: MessageStore, poller: MailboxPoller, params: dict[str, Any]) -> str:
    shipment = _shipment(store, params)
    if shipment.state != S.AWAITING_INFO:
        raise ActionError(f"Shipment {shipment.id} is {shipment.state.value}; nothing is outstanding from the customer.")
    missing = missing_fields(shipment.request)
    if not missing:
        raise ActionError("Nothing is missing any more.")
    limit = _settings(poller).max_clarifications
    if shipment.clarifications >= limit:  # the workflow would close the shipment instead of asking
        raise ActionError(f"The customer was already asked {limit} times, the limit (MAX_CLARIFICATIONS). Close the shipment, or raise the limit.")
    await poller.workflow._clarify(shipment, missing, shipment.id)  # noqa: SLF001 - the workflow's own question
    after = store.get_shipment(shipment.id)
    return f"Question {after.clarifications} of {limit} written to {after.customer}."


async def _close_shipment(store: MessageStore, poller: MailboxPoller, params: dict[str, Any]) -> str:
    shipment = _shipment(store, params)
    if shipment.state in (S.BOOKED, S.CONFIRMED, S.REJECTED, S.FAILED):
        raise ActionError(f"Shipment {shipment.id} is already {shipment.state.value}.")
    shipment.state, shipment.error = S.FAILED, CLOSED_BY_OPERATOR
    store.save_shipment(shipment)
    return f"Shipment {shipment.id} closed. No mail was sent."


async def _retry_mail(store: MessageStore, poller: MailboxPoller, params: dict[str, Any]) -> str:
    mail_id = _number(params, "mail_id")
    if not any(f["message_id"] == mail_id for f in store.message_failures()):
        raise ActionError(f"Mail {mail_id} is not among the failed mails.")
    stored = store.get_message(mail_id)
    if stored is not None and not stored.decision.reasoning.startswith(GAVE_UP):
        store.clear_failure(mail_id)  # it failed once and went through on a later attempt
        return f"Mail {mail_id} had already been processed after its failure ({stored.decision.category.value}); nothing to do."
    try:
        message = await poller._mailbox_client.get_message(mail_id)  # noqa: SLF001 - read it again from the mailbox
    except Exception as exc:  # noqa: BLE001 - reported to the operator
        raise ActionError(f"Mail {mail_id} could not be read from the mailbox: {exc}") from exc
    # The record written when the router gave up is replaced by the new reading. If that
    # fails again, the mail is stored as given up once more, so it is never lost.
    store.forget_message(mail_id)
    try:
        decision = await poller.process_message(message)
    except Exception as exc:  # noqa: BLE001 - reported to the operator
        store.record_failure(mail_id, str(exc))
        poller._give_up(message, str(exc))  # noqa: SLF001 - same record as after the automatic attempts
        raise ActionError(f"Mail {mail_id} failed again: {exc}") from exc
    store.clear_failure(mail_id)
    return f"Mail {mail_id} processed again, classified as {decision.category.value}."


_ACTIONS = {
    "retry_booking": _retry_booking,
    "remind_customer": _remind_customer,
    "close_shipment": _close_shipment,
    "retry_mail": _retry_mail,
}
