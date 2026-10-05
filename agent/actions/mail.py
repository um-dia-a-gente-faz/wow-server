"""Mailbox actions (UM-60).

Split out of the former agent/actions.py (issue #248).
"""

import time

from .. import mail as mailmod
from .. import update_fields as uf
from .base import (
    Action,
    ActionResult,
    DEFAULT_CONFIRM_POLL_S,
    DEFAULT_CONFIRM_TIMEOUT_S,
    _find_item_guid,
    _record_event,
    _wait_for,
    _wait_for_value,
    register,
    send,
)
from .chat import (chat_text_error, player_name_error)


# ── Mailbox (UM-60) ────────────────────────────────────────────────────────
# Wire layouts verified against TrinityCore branch `3.3.5`, see
# agent/mail.py's docstring for the sources.


def _find_nearby_mailbox(session, world):
    """Closest currently-perceived mailbox (ObjectInfo.is_mailbox()) within
    MAILBOX_INTERACT_RANGE_YD, or None. A gameobject's type only becomes
    known once its SMSG_GAMEOBJECT_QUERY_RESPONSE arrives (queued
    automatically the moment it's first perceived, same as its name) — a
    mailbox just perceived this instant may not be recognized yet."""
    if session.player_position is None:
        return None
    best = None
    best_dist = None
    for obj in world.get_objects().values():
        if not obj.is_mailbox():
            continue
        dist = obj.distance_to(session.player_position)
        if dist is None or dist > mailmod.MAILBOX_INTERACT_RANGE_YD:
            continue
        if best_dist is None or dist < best_dist:
            best, best_dist = obj, dist
    return best


def _mail_item_flags(world, bag: int, slot: int):
    """(item_guid, entry, is_soulbound) for the item at bag/slot, or None if
    there's no known item there. UM-59 (player trade) has the identical
    soulbound check in its own _resolve_offered_item — worth factoring into
    one shared helper instead of two independent copies."""
    item_guid = _find_item_guid(world, bag, slot)
    if item_guid is None:
        return None
    item_obj = world.get_object(item_guid)
    if item_obj is None:
        return None
    decoded = uf.decode_item_fields(item_obj.raw_fields) if item_obj.raw_fields else {}
    flags = decoded.get("item_flags") or 0
    is_soulbound = bool(flags & mailmod.ITEM_FIELD_FLAG_SOULBOUND)
    return item_guid, item_obj.entry, is_soulbound


@register
class OpenMailboxAction(Action):
    name = "open_mailbox"
    description = (f"Open the nearest mailbox within {mailmod.MAILBOX_INTERACT_RANGE_YD:.0f} yd "
                    "and request its inbox (check the perception snapshot's `mailbox` after "
                    "calling this).")
    params = {}
    required = ()
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, **_) -> str | None:
        if _find_nearby_mailbox(session, world) is None:
            return "no mailbox is within interact range — try move_towards a mailbox first"
        return None

    def execute(self, session, world, **_) -> ActionResult:
        mailbox = _find_nearby_mailbox(session, world)
        if mailbox is None:
            # Re-checked, not just trusted from check(): the mailbox is a
            # stationary gameobject, so this only fires on a genuine race
            # (e.g. it left perception via a concurrent OUT_OF_RANGE_OBJECTS
            # on the recv thread between check() and execute()) — found in
            # review. Reported as a normal ActionResult, not an
            # AttributeError, since that wouldn't be caught by think.py's
            # `except TypeError` and would crash the think cycle instead.
            return ActionResult(ok=False, error="mailbox no longer in range")
        world.open_mailbox_request(mailbox.guid)
        send(session, mailmod.CMSG_GET_MAIL_LIST, mailmod.build_get_mail_list(mailbox.guid))

        def has_list():
            current = world.get_mailbox()
            return current is not None and current.get("mails") is not None

        got_list = _wait_for(has_list, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"mailbox_guid": mailbox.guid, "mailbox": world.get_mailbox()}
        if not got_list:
            return ActionResult(ok=False, error="no mail list response seen (timed out)", detail=detail)
        return ActionResult(ok=True, detail=detail)


@register
class SendMailAction(Action):
    name = "send_mail"
    description = ("Send mail (with optional gold and one optional item) to a player by name — "
                    "works even if they're offline or far away. Costs "
                    f"{mailmod.MAIL_POSTAGE_COPPER} copper postage in addition to any gold sent. "
                    "Requires a nearby mailbox.")
    params = {
        "to": {"type": "string", "description": "Recipient character name."},
        "subject": {"type": "string", "description": "Mail subject."},
        "body": {"type": "string", "description": "Mail body text."},
        "gold": {"type": "integer", "description": "Copper to send. Default 0."},
        "bag": {"type": "integer", "description": "Bag byte of an item to attach (255 = equipped "
                                                    "items/backpack). Omit to send no item."},
        "slot": {"type": "integer", "description": "Inventory slot of the item to attach — "
                                                     "required together with bag."},
    }
    required = ("to", "subject", "body")
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, to: str, subject: str, body: str, gold: int = 0,
              bag: int | None = None, slot: int | None = None, **_) -> str | None:
        error = player_name_error(to, field="to") or chat_text_error(subject, field="subject")
        if error is not None:
            return error
        if _find_nearby_mailbox(session, world) is None:
            return "no mailbox is within interact range — try move_towards a mailbox first"
        if gold < 0:
            return "gold must be >= 0"
        item_guid = None
        if (bag is None) != (slot is None):
            return "bag and slot must be given together"
        if bag is not None:
            resolved = _mail_item_flags(world, bag, slot)
            if resolved is None:
                return f"no known item at bag={bag} slot={slot}"
            item_guid, _entry, is_soulbound = resolved
            if is_soulbound:
                return "item is soulbound and can't be mailed"
        postage = mailmod.MAIL_POSTAGE_COPPER
        have = getattr(session, "coinage", 0) or 0
        if postage + gold > have:
            return f"not enough gold (have {have}, need {postage + gold} including postage)"
        return None

    def execute(self, session, world, to: str, subject: str, body: str, gold: int = 0,
                bag: int | None = None, slot: int | None = None, **_) -> ActionResult:
        mailbox = _find_nearby_mailbox(session, world)
        if mailbox is None:
            # Same TOCTOU guard as OpenMailboxAction.execute() — found in review.
            return ActionResult(ok=False, error="mailbox no longer in range")
        item_guid = None
        if bag is not None:
            # Re-checked, not just trusted from check() — found in review:
            # the item could be moved/consumed by a concurrent inventory
            # update on the recv thread between check() and execute(). A
            # bare unpack of None would raise TypeError, which think.py
            # *does* catch (unlike the mailbox case above) but mislabels
            # as "bad params" — misleading, since the LLM's params were
            # fine when it called this.
            resolved = _mail_item_flags(world, bag, slot)
            if resolved is None:
                return ActionResult(ok=False, error=f"item at bag={bag} slot={slot} is no longer there")
            item_guid, _entry, _is_soulbound = resolved
        sent_at = time.monotonic()
        send(session, mailmod.CMSG_SEND_MAIL,
                              mailmod.build_send_mail(mailbox.guid, to, subject, body,
                                                       money=gold, item_guid=item_guid))

        def find_result():
            for e in session.events:
                if e.get("t", 0) < sent_at or e.get("kind") != "mail_result":
                    continue
                if e.get("command") == mailmod.MAIL_SEND:
                    return e
            return None

        outcome = _wait_for_value(find_result, timeout=self.confirm_timeout,
                                   interval=self.confirm_interval)
        detail = {"to": to, "subject": subject, "gold": gold, "bag": bag, "slot": slot}
        if outcome is None:
            _record_event(session, "mail_error", reason="no send-mail confirmation seen (timed out)", to=to)
            return ActionResult(ok=False, error="no send-mail confirmation seen (timed out)", detail=detail)
        detail["outcome"] = outcome
        if outcome["error_code"] != mailmod.MAIL_OK:
            _record_event(session, "mail_error", reason=outcome["error_name"], to=to)
            return ActionResult(ok=False, error=outcome["error_name"], detail=detail)
        summary = {"to": to, "subject": subject, "gold": gold, "item_attached": bag is not None}
        _record_event(session, "mail_sent", to=to, summary=summary)
        return ActionResult(ok=True, detail=detail)


@register
class TakeMailAction(Action):
    name = "take_mail"
    description = ("Take all money and every item attached to mail_id from the currently open "
                    "mailbox window (call open_mailbox first) — v1 take-everything, no selective "
                    "taking, same as loot().")
    params = {
        "mail_id": {"type": "integer", "description": "Mail id, from the open mailbox window's "
                                                        "`mails` list."},
    }
    required = ("mail_id",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, mail_id: int, **_) -> str | None:
        mailbox = world.get_mailbox()
        if mailbox is None or mailbox.get("mails") is None:
            return "no mailbox window is open — call open_mailbox first"
        mail = next((m for m in mailbox["mails"] if m["mail_id"] == mail_id), None)
        if mail is None:
            return f"mail_id {mail_id} is not in the open mailbox window"
        # A COD amount is charged automatically when taking the item(s) —
        # HandleMailTakeItem checks HasEnoughMoney(m->COD) server-side.
        if mail["attachments"] and mail["cod"] > (getattr(session, "coinage", 0) or 0):
            return (f"not enough gold to pay this mail's {mail['cod']} copper COD "
                     f"(have {getattr(session, 'coinage', 0) or 0})")
        return None

    def execute(self, session, world, mail_id: int, **_) -> ActionResult:
        mailbox = world.get_mailbox()
        mail = next((m for m in (mailbox or {}).get("mails") or [] if m["mail_id"] == mail_id), None)
        if mail is None:
            # Re-checked, not just trusted from check() — found in review:
            # a concurrent SMSG_MAIL_LIST_RESULT (recv thread) could replace
            # world.mailbox between check() and execute(). A bare next()
            # would raise StopIteration here, which think.py's `except
            # TypeError` doesn't catch, crashing the think cycle instead of
            # degrading to a normal ActionResult(ok=False).
            return ActionResult(ok=False, error=f"mail_id {mail_id} is no longer in the open mailbox window")

        def find_result(command, sent_at):
            def _find():
                for e in session.events:
                    if e.get("t", 0) < sent_at or e.get("kind") != "mail_result":
                        continue
                    if e.get("mail_id") == mail_id and e.get("command") == command:
                        return e
                return None
            return _find

        outcomes = {"money": None, "items": []}
        if mail["money"]:  # COD is charged automatically alongside taking an item, not via TAKE_MONEY
            sent_at = time.monotonic()
            send(session, mailmod.CMSG_MAIL_TAKE_MONEY,
                                  mailmod.build_mail_take_money(mailbox["mailbox_guid"], mail_id))
            outcomes["money"] = _wait_for_value(find_result(mailmod.MAIL_MONEY_TAKEN, sent_at),
                                                 timeout=self.confirm_timeout,
                                                 interval=self.confirm_interval)

        for att in mail["attachments"]:
            # A fresh sent_at per attachment, not a shared one, matches by
            # time order instead of the response's `attach_id` — found in
            # review: SMSG_SEND_MAIL_RESULT only includes `attach_id` on
            # success (or item-expired); a same-mail_id failure for a
            # *different* reason (e.g. equip error) carries no attach_id at
            # all, so attach_id-matching would never see it and this
            # attachment would time out instead of surfacing the real
            # error. Safe because each take-item send blocks on its own
            # reply before the next is sent (sequential, not pipelined), so
            # "the next mail_result for this mail_id/command" is
            # unambiguous.
            sent_at = time.monotonic()
            send(session, mailmod.CMSG_MAIL_TAKE_ITEM,
                                  mailmod.build_mail_take_item(mailbox["mailbox_guid"], mail_id,
                                                                att["attach_id"]))
            outcomes["items"].append(_wait_for_value(
                find_result(mailmod.MAIL_ITEM_TAKEN, sent_at),
                timeout=self.confirm_timeout, interval=self.confirm_interval))

        detail = {"mail_id": mail_id, "outcomes": outcomes}
        money_ok = outcomes["money"] is None or outcomes["money"].get("error_code") == mailmod.MAIL_OK
        items_ok = all(o is not None and o.get("error_code") == mailmod.MAIL_OK for o in outcomes["items"])
        if not money_ok or not items_ok:
            return ActionResult(ok=False, error="one or more mail attachments failed to take", detail=detail)
        return ActionResult(ok=True, detail=detail)


@register
class DeleteMailAction(Action):
    name = "delete_mail"
    description = ("Delete mail_id from the currently open mailbox window (call open_mailbox "
                    "first). Irreversible — take any attachments first, they're lost otherwise.")
    params = {
        "mail_id": {"type": "integer", "description": "Mail id, from the open mailbox window's "
                                                        "`mails` list."},
    }
    required = ("mail_id",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, mail_id: int, **_) -> str | None:
        mailbox = world.get_mailbox()
        if mailbox is None or mailbox.get("mails") is None:
            return "no mailbox window is open — call open_mailbox first"
        if not any(m["mail_id"] == mail_id for m in mailbox["mails"]):
            return f"mail_id {mail_id} is not in the open mailbox window"
        return None

    def execute(self, session, world, mail_id: int, **_) -> ActionResult:
        mailbox = world.get_mailbox()
        sent_at = time.monotonic()
        send(session, mailmod.CMSG_MAIL_DELETE,
                              mailmod.build_mail_delete(mailbox["mailbox_guid"], mail_id))

        def find_result():
            for e in session.events:
                if e.get("t", 0) < sent_at or e.get("kind") != "mail_result":
                    continue
                if e.get("mail_id") == mail_id and e.get("command") == mailmod.MAIL_DELETED:
                    return e
            return None

        outcome = _wait_for_value(find_result, timeout=self.confirm_timeout,
                                   interval=self.confirm_interval)
        detail = {"mail_id": mail_id, "outcome": outcome}
        if outcome is None:
            return ActionResult(ok=False, error="no delete confirmation seen (timed out)", detail=detail)
        if outcome["error_code"] != mailmod.MAIL_OK:
            # e.g. a COD mail can't be deleted before it's paid/returned (MAIL_ERR_INTERNAL_ERROR)
            return ActionResult(ok=False, error=outcome["error_name"], detail=detail)
        return ActionResult(ok=True, detail=detail)
