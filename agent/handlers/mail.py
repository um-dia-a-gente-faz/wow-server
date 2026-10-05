"""Mailbox packets (UM-60)."""

import struct

from .. import mail as mail_mod
from .. import perception as per
from ..router import ROUTER


def handle_send_mail_result(ctx, payload: bytes):
    """Answers send_mail, and both halves of take_mail (money then
    each item) and delete_mail — `data['command']` (MailResponseType)
    says which. Recorded as a raw 'mail_result' event; agent/actions/mail.py
    correlates by mail_id/command and, for send_mail specifically,
    additionally records the ticket-named mail_sent/mail_error event
    once it knows the recipient (not present in this payload)."""
    ctx.dump(mail_mod.SMSG_SEND_MAIL_RESULT, payload)
    try:
        data = mail_mod.parse_send_mail_result(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_SEND_MAIL_RESULT ({len(payload)} B): {e}") from e
    ctx.state.record_event("mail_result", **data)


def handle_mail_list_result(ctx, payload: bytes):
    ctx.dump(mail_mod.SMSG_MAIL_LIST_RESULT, payload)
    try:
        data = mail_mod.parse_mail_list_result(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_MAIL_LIST_RESULT ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_mail_list_result(data)


def handle_received_mail(ctx, payload: bytes):
    ctx.dump(mail_mod.SMSG_RECEIVED_MAIL, payload)
    try:
        data = mail_mod.parse_received_mail(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_RECEIVED_MAIL ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_received_mail(data)
    ctx.state.record_event("mail_received", **data)


ROUTER.register_all({
    mail_mod.SMSG_SEND_MAIL_RESULT: handle_send_mail_result,
    mail_mod.SMSG_MAIL_LIST_RESULT: handle_mail_list_result,
    mail_mod.SMSG_RECEIVED_MAIL: handle_received_mail,
})
