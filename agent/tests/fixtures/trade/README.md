# SMSG_TRADE_STATUS / SMSG_TRADE_STATUS_EXTENDED fixtures

Real, live-captured payloads from `192.168.1.64`, characters **Farstrider**
(account `AGENT02`, hunter) and **Shadowblade** (account `AGENT03`, rogue),
both at the Sunstrider Isle start area (map 530), 2026-09-17. Captured with
`AGENT_DUMP_PACKETS` (UM-32) driving both characters through a full trade —
open, accept the request, offer an item (`Tattered Pelt`, entry `20812`),
accept on both sides — via `agent.actions.REGISTRY`, then a second run that
opened a trade, accepted on one side, changed the offer (to confirm the
accept resets — UM-59's acceptance criterion), then cancelled.

Each file is the raw application payload handed to
`agent.trade.parse_trade_status`/`parse_trade_status_extended` — i.e. after
opcode/length framing is stripped, before any other parsing.

No account/session secrets are present in any fixture — these are game-state
payloads only (guids, item entries/display ids, a trade money amount that
was always 0 in this session — neither character had gold to offer).

## Files

| File | Status | Size | Notes |
|---|---|---:|---|
| `trade_status_begin_trade.bin` | `TRADE_STATUS_BEGIN_TRADE` (1) | 12 B | Sent to Shadowblade after Farstrider's `CMSG_INITIATE_TRADE`. `trader_guid` decodes to `3` (Farstrider's own guid this session). |
| `trade_status_open_window.bin` | `TRADE_STATUS_OPEN_WINDOW` (2) | 8 B | Sent to both sides after Shadowblade's `CMSG_BEGIN_TRADE`. |
| `trade_status_back_to_trade.bin` | `TRADE_STATUS_BACK_TO_TRADE` (7) | 4 B | Sent to both sides after Farstrider's `CMSG_SET_TRADE_ITEM` (offering the pelt) — confirms `TradeData::SetItem`'s unconditional un-accept on both `TradeData` objects. |
| `trade_status_trade_accept.bin` | `TRADE_STATUS_TRADE_ACCEPT` (4) | 4 B | Sent to the partner after the other side's `CMSG_ACCEPT_TRADE` (while the receiver hadn't accepted yet). |
| `trade_status_trade_canceled.bin` | `TRADE_STATUS_TRADE_CANCELED` (3) | 4 B | Sent to both sides after `CMSG_CANCEL_TRADE` (second live run, after the accept-reset check). |
| `trade_status_trade_complete.bin` | `TRADE_STATUS_TRADE_COMPLETE` (8) | 4 B | Sent to both sides once both had accepted and the item transfer succeeded — confirmed via the DB afterward: the pelt (`item_guid 22`) moved from Farstrider's inventory to Shadowblade's. |
| `trade_status_extended_with_item.bin` | `SMSG_TRADE_STATUS_EXTENDED`, `is_trader_data=1` | 532 B | Sent to Shadowblade right after Farstrider's `offer_item` — slot 0 has the pelt (`entry=20812`, `display_id=33222`, `count=1`), the other 6 slots empty. Exact size matches `1 + 4*5 + 7*(1 + 4*18)`, confirming the fixed 7-slot layout. |
| `trade_status_extended_empty.bin` | `SMSG_TRADE_STATUS_EXTENDED`, `is_trader_data=1` | 532 B | Sent to Farstrider describing Shadowblade's (still empty) offer — all 7 slots zeroed. |
