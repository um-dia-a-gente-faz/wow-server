"""Trade packets (UM-59)."""

from .. import trade as trade_mod


def handle_trade_status(ctx, payload: bytes):
    ctx.dump(trade_mod.SMSG_TRADE_STATUS, payload)
    data = trade_mod.parse_trade_status(payload)
    # Recorded unconditionally (not just when apply_trade_status returns
    # a named event) — agent/actions/trade.py's offer_item/offer_gold/open_trade/
    # accept_trade_request/cancel_trade wait on this raw status_name
    # directly (e.g. "back_to_trade" confirms our own SET_TRADE_ITEM,
    # which never gets its own named event — see agent/trade.py).
    ctx.state.record_event("trade_status", **data)
    result = ctx.state.world_state.apply_trade_status(data)
    if result is not None:
        kind, fields = result
        ctx.state.record_event(kind, **fields)


def handle_trade_status_extended(ctx, payload: bytes):
    ctx.dump(trade_mod.SMSG_TRADE_STATUS_EXTENDED, payload)
    data = trade_mod.parse_trade_status_extended(payload)
    changed = ctx.state.world_state.apply_trade_status_extended(data)
    if changed is not None:
        ctx.state.record_event("trade_offer_changed", **changed)


def register(router):
    router.register_all({
        trade_mod.SMSG_TRADE_STATUS: handle_trade_status,
        trade_mod.SMSG_TRADE_STATUS_EXTENDED: handle_trade_status_extended,
    })
