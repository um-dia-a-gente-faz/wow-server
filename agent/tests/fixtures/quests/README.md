# Quest fixtures

Real payloads captured live from `192.168.1.64` on 2026-09-24. Character: **Sunspeaker**
(account `AGENT04`, priest, level 1, Sunstrider Isle start area, map 530). The script
logged in, used `interact` on Magistrix Erona, ran `accept_quest` for quest 8325
"Reclaiming Sunstrider Isle", then used `auto_attack` on a Mana Wyrm. It wrapped the
session's quest handlers so each raw payload was written to disk before parsing.

Each file is the raw application payload: opcode and length framing are stripped, and
nothing else has been parsed. The files contain game state only (quest text, entries
and a creature guid), with no account or session secrets.

## Files

| File | Opcode | Size | Notes |
|---|---|---:|---|
| `quest_query_response_8325.bin` | `SMSG_QUEST_QUERY_RESPONSE` (`0x05D`) | 1082 B | Reply to the agent's `CMSG_QUEST_QUERY` for 8325 once the quest reached the log. Layout matches `QueryQuestInfoResponse::Write` exactly. Values match `world.quest_template` for ID 8325: QuestSortID 3431, Flags 524424, RewardMoney 30, reward choices 20997/20998, faction 911 +5, RequiredNpcOrGo1 15274 (Mana Wyrm) x8. The old parser read QuestSortID 3431 (`67 0D 00`) as the title, which produced `"g\r"`. |
| `questupdate_add_kill_8325.bin` | `SMSG_QUESTUPDATE_ADD_KILL` (`0x199`) | 24 B | Sent for Sunspeaker's first Mana Wyrm kill: quest 8325, entry 15274, count 1, required 8, victim guid `0xF130003BAA002500`. The same kill set the quest-log counter 0 to 1 (`PLAYER_QUEST_LOG_1_3`), and the snapshot showed `Mana Wyrm slain: 1/8`. |
