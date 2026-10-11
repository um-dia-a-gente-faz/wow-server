"""Mailbox packets (UM-60)."""

from .. import mail as mail_mod


def handle_send_mail_result(ctx, payload: bytes):
    """Answers send_mail, and both halves of take_mail (money then
    each item) and delete_mail — `data['command']` (MailResponseType)
    says which. Recorded as a raw 'mail_result' event; agent/actions/mail.py
    correlates by mail_id/command and, for send_mail specifically,
    additionally records the ticket-named mail_sent/mail_error event
    once it knows the recipient (not present in this payload)."""
    ctx.dump(mail_mod.SMSG_SEND_MAIL_RESULT, payload)
    data = mail_mod.parse_send_mail_result(payload)
    ctx.state.record_event("mail_result", **data)


def handle_mail_list_result(ctx, payload: bytes):
    ctx.dump(mail_mod.SMSG_MAIL_LIST_RESULT, payload)
    data = mail_mod.parse_mail_list_result(payload)
    ctx.state.world_state.apply_mail_list_result(data)


def handle_received_mail(ctx, payload: bytes):
    ctx.dump(mail_mod.SMSG_RECEIVED_MAIL, payload)
    data = mail_mod.parse_received_mail(payload)
    ctx.state.world_state.apply_received_mail(data)
    ctx.state.record_event("mail_received", **data)


def register(router):
    router.register_all({
        mail_mod.SMSG_SEND_MAIL_RESULT: handle_send_mail_result,
        mail_mod.SMSG_MAIL_LIST_RESULT: handle_mail_list_result,
        mail_mod.SMSG_RECEIVED_MAIL: handle_received_mail,
    })
