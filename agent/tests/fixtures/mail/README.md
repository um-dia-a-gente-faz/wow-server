# SMSG_SEND_MAIL_RESULT / SMSG_MAIL_LIST_RESULT fixtures

Real, live-captured payloads from `192.168.1.64`, character **Luaprata**
(account `AGENT01`, paladin), 2026-09-17. Captured with `AGENT_DUMP_PACKETS`
driving `agent.actions.REGISTRY`/raw `session._send_packet` calls against
the Silvermoon City mailbox (gameobject entry `182363`, ~182 yd from
Luaprata's login position — reached with a single `move_to`).

No account/session secrets are present in either fixture — these are
game-state payloads only.

## Files

| File | Opcode | Size | Notes |
|---|---|---:|---|
| `send_mail_result_not_enough_money.bin` | `SMSG_SEND_MAIL_RESULT` (`0x239`) | 12 B | Sent after a raw `CMSG_SEND_MAIL` with `money=0` — Luaprata had 0 copper, less than the 30-copper flat postage (`agent/mail.py`'s `MAIL_POSTAGE_COPPER`, confirmed against `HandleSendMail`'s `cost` calc). `command=0` (`MAIL_SEND`), `error_code=3` (`MAIL_ERR_NOT_ENOUGH_MONEY`) — confirms `build_send_mail`'s byte layout is accepted by the real server (a malformed packet would desync the stream or get silently dropped, not produce this specific, correct error). `agent.actions.SendMailAction.check()` already rejects this case client-side before ever sending — this fixture is what the *server* itself would say if that check were bypassed. |
| `mail_list_result_empty.bin` | `SMSG_MAIL_LIST_RESULT` (`0x23B`) | 5 B | Real response to `CMSG_GET_MAIL_LIST` for Luaprata's (empty) mailbox — `total_records=0`, 0 mail entries. Confirms `CMSG_GET_MAIL_LIST`'s byte layout and `open_mailbox()`'s full round trip end to end. |

**Not captured this session** (no character had ≥30 copper — the minimum
mail costs *anything at all*, even a text-only letter with no gold or item
attached): a successful `SMSG_SEND_MAIL_RESULT` (`MAIL_OK`), a non-empty
`SMSG_MAIL_LIST_RESULT` entry with attachments, `SMSG_SEND_MAIL_RESULT` for
`MAIL_MONEY_TAKEN`/`MAIL_ITEM_TAKEN`/`MAIL_DELETED`, and `SMSG_RECEIVED_MAIL`.
`agent/tests/test_mail.py`'s hand-built-byte tests cover all of those
layouts; only the live round-trip through a real server wasn't exercised
for them.
