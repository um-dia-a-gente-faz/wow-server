# SMSG_MESSAGECHAT captures (UM-47)

Raw, decrypted `SMSG_MESSAGECHAT` (0x096) payloads — everything after the
6-byte world header — captured off the live realm (192.168.1.64, TrinityCore
3.3.5a build 12340) on 2026-09-25. Hex, whitespace-insensitive; everything
from the first `#` is a comment.

Captured by logging **Farstrider** (AGENT02, guid 3) and **Shadowblade**
(AGENT03, guid 4) in with `agent/`, wrapping `WoWSession._handle_messagechat`
to record `payload.hex()`, and having Farstrider say/yell/emote/channel-say
and whisper Shadowblade. No GM commands were used, and the worldserver was
not touched.

| File | Kind | Recorded by |
|---|---|---|
| `say.hex` | `CHAT_MSG_SAY` (0x01) | Farstrider (hears itself) |
| `say_non_ascii.hex` | `CHAT_MSG_SAY`, UTF-8 `Olá! Já estou a caminho — vamos?` | Farstrider |
| `yell.hex` | `CHAT_MSG_YELL` (0x06) | Farstrider |
| `emote.hex` | `CHAT_MSG_EMOTE` (0x0A) | Farstrider |
| `channel.hex` | `CHAT_MSG_CHANNEL` (0x11), `General - Silvermoon City` | Farstrider |
| `whisper.hex` | `CHAT_MSG_WHISPER` (0x07) | Shadowblade (the recipient) |
| `whisper_inform.hex` | `CHAT_MSG_WHISPER_INFORM` (0x09) | Farstrider (the sender's echo) |

## What these bytes settle

**`TargetGUID` is not the listener, and not the addressee either.** Every
capture here has `TargetGUID == SenderGUID`. That is not a coincidence:
`WorldPackets::Chat::Chat::Initialize(chatType, language, sender, receiver, …)`
writes `receiver->GetGUID()` into `TargetGUID` (ChatPackets.cpp), and
TrinityCore passes the *same object twice* for all of this:

- `Player::Say` / `Player::Yell` / `Player::TextEmote` → `Initialize(…, this, this, …)`
- `Player::Whisper` → `Initialize(CHAT_MSG_WHISPER, …, this, this, …)` to the
  target, then `Initialize(CHAT_MSG_WHISPER_INFORM, …, target, target, …)` to
  the sender

So for a whisper the *sender's* echo carries the **addressee** in
`SenderGUID` (`whisper_inform.hex` → guid 4 = Shadowblade), and the
recipient's copy carries the **whisperer** (`whisper.hex` → guid 3). There is
no field anywhere that names the listener. `agent/chat_relay.py` relies on
this; see `docs/PROTOCOL-NOTES.md`.
