"""What an operator can do from the CRM, each through an operation the workflow already has.

Every action except `dismiss` and `restore` changes a shipment or sends mail, so it is only
allowed in the process that runs the mailbox poller: that process owns the state machine.
A second process looking at the same database (the read-only viewer) must not book or send
behind its back. Actions take the poller's work lock, so they never run in between two
steps of a poll cycle. Whether mail really leaves is still decided by SEND_ENABLED.
"""

from typing import Any

from app.extractors import missing_fields
from app.poller import MailboxPoller
from app.schemas import ShipmentState
from app.storage import MessageStore

S = ShipmentState
CLOSED_BY_OPERATOR = "closed by operator"
ANYWHERE = {"dismiss", "restore"}  # only touch the CRM's own table


class ActionError(Exception):
    """The action does not apply (wrong state, nothing to act on, not allowed here)."""


def may_act(poller: MailboxPoller | None) -> bool:
    return bool(poller and poller.running and poller.workflow)


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


def _shipment(store: MessageStore, params: dict[str, Any]):
    shipment = store.get_shipment(int(params.get("shipment_id") or 0))
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
    await poller.workflow._clarify(shipment, missing, shipment.id)  # noqa: SLF001 - the workflow's own question
    after = store.get_shipment(shipment.id)
    if after.state == S.FAILED:
        return f"No further question sent: the limit of clarifications is reached. Shipment {after.id} is now FAILED."
    return f"Clarification {after.clarifications} written to {after.customer}."


async def _close_shipment(store: MessageStore, poller: MailboxPoller, params: dict[str, Any]) -> str:
    shipment = _shipment(store, params)
    if shipment.state in (S.BOOKED, S.CONFIRMED, S.REJECTED, S.FAILED):
        raise ActionError(f"Shipment {shipment.id} is already {shipment.state.value}.")
    shipment.state, shipment.error = S.FAILED, CLOSED_BY_OPERATOR
    store.save_shipment(shipment)
    return f"Shipment {shipment.id} closed. No mail was sent."


async def _retry_mail(store: MessageStore, poller: MailboxPoller, params: dict[str, Any]) -> str:
    mail_id = int(params.get("mail_id") or 0)
    if not any(f["message_id"] == mail_id for f in store.message_failures()):
        raise ActionError(f"Mail {mail_id} is not among the failed mails.")
    message = await poller._mailbox_client.get_message(mail_id)  # noqa: SLF001 - read it again from the mailbox
    store.clear_failure(mail_id)
    decision = await poller.process_message(message)
    return f"Mail {mail_id} processed again, classified as {decision.category.value}."


_ACTIONS = {
    "retry_booking": _retry_booking,
    "remind_customer": _remind_customer,
    "close_shipment": _close_shipment,
    "retry_mail": _retry_mail,
}
