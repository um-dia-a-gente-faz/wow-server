"""Candidate generator (UM-97): turn one perception snapshot into a short,
bounded list of concrete actions a choice-only brain (Jev, UM-95/UM-99) can
pick from.

The LLM path (agent/llm.py) hands the model the whole action catalog as
tools and lets it fill in arguments. Jev works the other way round: it only
picks one option from a list we supply and never writes arguments. So the
arguments are decided here, in code, from `world.snapshot()`.

Interface
---------

    generate(snapshot, *, my_guid=None, reflex_state=None, history=None,
             limit=MAX_CANDIDATES, handles=None, notes=None)
        -> list[dict]

Each candidate is a plain JSON-serialisable dict:

    {"id": "auto_attack:guid=4660",            # unique in the list, stable across cycles
     "label": "attack Mana Wyrm (level 3, 3.1 yd, attacking you)",
     "action": "auto_attack",                  # a name in agent.actions.REGISTRY
     "params": {"guid": 4660}}                 # exact kwargs for action.run()

- `id` is derived only from `action` + `params`, so the same option keeps the
  same id from one cycle to the next. Use it as Jev's `criteria` key.
- `label` is a short description for the model (and the audit log).
- `params` never contains free text: every value is an int, float or bool,
  or a string copied verbatim from the snapshot (a nearby player's name for
  `follow`). Nothing here composes chat, mail or gossip codes.
- The list is ordered by priority (open window, invite, threats, loot,
  quest givers, attack options, follow, idle) and capped at `limit`. The
  last entry is always `idle`, so there is always a valid choice.

Selection rules, briefly: units attacking the agent come first; lootable
corpses and quest givers worth visiting (quest available / reward ready)
within APPROACH_MAX_YD; unprovoked attack options only for units with no
npc_flags, not in someone else's fight, and at most ATTACK_LEVEL_MARGIN
levels above the agent (so none at all while the agent's own level is
unknown). Anything out of reach becomes a short `move_towards` hop.

`snapshot` is what agent/think.py builds: `world.snapshot(...)` updated with
`_self_status()` (the `me` key). `my_guid` (session.my_guid / world.my_guid)
lets the generator tell which units are attacking the agent. `reflex_state`
is the dict agent/__main__.py's `_reflex_state()` returns (its "follow"
entry decides between follow and stop_following/assist options).

UM-89: snapshot GUID fields may hold short handle strings ("u3") instead of
raw ints — that is what WorldState.snapshot() produces. `handles` (the
world's HandleMap, passed through from think.py via Brain.decide) resolves
them back to ints for the comparisons below (whose fight is it, what is
attacking me). Candidate params keep the handle strings as the snapshot
carries them: think.py's resolve_params maps them back before the action
runs, so a handle and a raw int both work end to end.

`history` is ThinkState.for_prompt() (oldest first). It makes selection
history-aware (GH-166), conservatively:

- drop: an option whose latest attempt in the last HISTORY_DROP_WINDOW
  entries was rejected for a reason that retrying cannot fix (missing or bad
  params, unknown handle/action), or that failed the same way twice in a row.
  Entries older than the window no longer count, so a drop expires.
- demote (move behind the others, not remove): an option that succeeded but
  changed nothing (`changed is False`) HISTORY_DEMOTE_REPEATS times in a row
  at the end of the history.
- never touched: options aimed at a unit attacking the agent, and `idle`.

Malformed history entries are skipped. `history=None` or `[]` leaves the
output unchanged. Pass a list as `notes` to receive one dict per
dropped/demoted option ({"id", "effect", "reason"}) for the audit log.

Candidates only reference actions that are already registered: the follow
reflex's `follow`/`assist`/`stop_following` (agent/reflexes/follow.py) and
`idle` (agent/actions/combat.py). Each action's own `check()` still runs when the
chosen candidate is executed, so a stale candidate fails safely.
"""

from .handles import UnknownHandle
from . import action_names as A, item_compare
from .rules import APPROACH_MAX_YD, ATTACK_LEVEL_MARGIN, INTERACT_RANGE_YD, MELEE_RANGE_YD

MAX_CANDIDATES = 15


MAX_THREATS = 3
MAX_LOOT = 3
MAX_QUEST_GIVERS = 3
MAX_ATTACK = 3
MAX_FOLLOW = 2
MAX_GOSSIP_OPTIONS = 4
MAX_ABANDON = 2
MAX_SPELLS = 3
# Healing spells (agent.spells.SPELL_TABLE: Holy Light, Lesser Heal), self-cast when hurt.
HEAL_SPELL_IDS = frozenset({635, 2050})
MAX_TRAIN = 3
MAX_EQUIP = 3
MAX_USE_ITEMS = 2
MAX_SELL = 3

# ItemTemplate.InventoryType -> Player equipment slot(s), following
# EQUIPMENT_SLOT_* in TrinityCore. Paired slots are deliberately both checked.
_EQUIP_SLOTS = {
    1: (0,), 2: (1,), 3: (2,), 4: (3,), 5: (4,), 6: (5,), 7: (6,),
    8: (7,), 9: (8,), 10: (9,), 11: (10, 11), 12: (12, 13),
    13: (15, 16), 14: (16,), 15: (17,), 16: (14,), 17: (15, 16),
    19: (18,), 20: (4,), 21: (15,), 22: (16,), 23: (16,),
    25: (17,), 26: (17,), 28: (17,),
}
_BACKPACK_FIRST, _BACKPACK_LAST = 23, 38


def _backpack(item: dict) -> bool:
    return isinstance(item.get("slot"), int) and _BACKPACK_FIRST <= item["slot"] <= _BACKPACK_LAST


def _resource_pct(me: dict, resource: str) -> float | None:
    value = me.get(resource)
    if isinstance(value, str) and "/" in value:
        try:
            current, maximum = (float(part) for part in value.split("/", 1))
            return current / maximum if maximum > 0 else None
        except ValueError:
            return None
    return None

# Registered actions deliberately absent from the candidate generator. Kept
# adjacent to it so registry growth is visible and the coverage test pins it.
NOT_OFFERED = {
    A.SET_TARGET: "combat micro; auto_attack selects targets",
    A.FACE: "combat micro; cast and movement actions orient as needed",
    A.STOP_ATTACK: "combat micro; no levelling decision currently needs it",
    A.STOP_MOVEMENT: "combat micro; no levelling decision currently needs it",
    A.BUY_ITEM: "buy nothing by default until an item purchasing policy is decided",
    A.DESTROY_ITEM: "destructive inventory action is intentionally excluded",
    A.COMPARE_ITEMS: "comparison is used internally to offer equip upgrades",
    A.REST: "reflex-controlled survival action, not an explicit candidate",
    A.INVITE_TO_GROUP: "social grouping is out of scope",
    A.DECLINE_GROUP: "Jev accepts invites; declining is an LLM-brain decision (GH-71)",
    A.LEAVE_GROUP: "group management is an LLM-brain decision (GH-71)",
    A.PROMOTE_LEADER: "group management is an LLM-brain decision (GH-71)",
    A.OPEN_TRADE: "trade family is out of scope",
    A.ACCEPT_TRADE_REQUEST: "trade family is out of scope",
    A.OFFER_ITEM: "trade family is out of scope",
    A.OFFER_GOLD: "trade family is out of scope",
    A.ACCEPT_TRADE: "trade family is out of scope",
    A.CANCEL_TRADE: "trade family is out of scope",
    A.OPEN_MAILBOX: "mail family is out of scope",
    A.SEND_MAIL: "mail family is out of scope",
    A.TAKE_MAIL: "mail family is out of scope",
    A.DELETE_MAIL: "mail family is out of scope",
}

# agent.quests.QUEST_GIVER_STATUS_NAMES values worth walking over for.
_QUEST_GIVER_WORTH_VISITING = {"available": "has a quest", "reward": "quest ready to turn in",
                               "reward_rep": "quest ready to turn in"}


def candidate(action: str, params: dict, label: str) -> dict:
    """Build one candidate with its stable id."""
    key = ",".join(f"{k}={params[k]}" for k in sorted(params))
    return {"id": f"{action}:{key}" if key else action, "label": label,
            "action": action, "params": dict(params)}


def _name(unit: dict) -> str:
    if unit.get("name"):
        return unit["name"]
    guid = unit.get("guid")
    # guid may be a handle string ("u3") on an encoded snapshot (UM-89).
    return f"unit {guid:#x}" if isinstance(guid, int) else f"unit {guid or 'unknown'}"


def _guid_value(unit: dict, key: str, handles) -> int | None:
    """The int GUID behind a unit field, whether the snapshot carries it as
    a raw int or as a short handle string (UM-89), so the comparisons in
    generate() work on both shapes. None when it is neither."""
    value = unit.get(key)
    if isinstance(value, str):
        if handles is None:
            return None
        try:
            return handles.resolve(value)
        except UnknownHandle:
            return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dist(unit: dict) -> float:
    d = unit.get("distance")
    return d if isinstance(d, (int, float)) else float("inf")


def _alive(unit: dict) -> bool:
    hp = unit.get("health_pct")
    return hp is None or hp > 0


def _my_level(snapshot: dict):
    level = (snapshot.get("me") or {}).get("level")
    return level if isinstance(level, int) else None


def _engage(unit: dict, why: str) -> dict:
    """auto_attack when in melee range, else a short approach."""
    desc = f"{_name(unit)} (level {unit.get('level', '?')}, {_dist(unit):.1f} yd{', ' + why if why else ''})"
    if _dist(unit) <= MELEE_RANGE_YD:
        return candidate(A.AUTO_ATTACK, {"guid": unit["guid"]}, f"attack {desc}")
    return candidate(A.MOVE_TOWARDS, {"guid": unit["guid"], "stop_distance": 3.0},
                     f"move to attack {desc}")


def _window_candidates(snapshot: dict) -> list:
    window = snapshot.get("window")
    if not isinstance(window, dict):
        return []
    kind = window.get("kind")
    npc_guid = window.get("npc_guid")
    in_log = {q.get("quest_id") for q in snapshot.get("quest_log") or []}
    out = []

    if kind == "trainer" and npc_guid is not None:
        known = {s.get("id") for s in snapshot.get("spells") or []}
        for spell in (window.get("spells") or [])[:MAX_TRAIN]:
            if spell.get("spell_id") not in known:
                out.append(candidate(A.TRAIN_SPELL, {"trainer_guid": npc_guid, "spell_id": spell["spell_id"]},
                                     f"train spell {spell['spell_id']}"))
    if kind == "vendor" and npc_guid is not None:
        inventory = snapshot.get("inventory") or []
        for item in inventory:
            if item.get("template", {}).get("quality") == 0 and _backpack(item):
                out.append(candidate(A.SELL_ITEM, {"vendor_guid": npc_guid, "bag": 255,
                                                     "slot": item["slot"]},
                                     f"sell grey item {item.get('name') or item.get('entry')}"))
                if sum(c["action"] == A.SELL_ITEM for c in out) >= MAX_SELL:
                    break

    if kind == "quest_details" and npc_guid is not None:
        out.append(candidate(A.ACCEPT_QUEST, {"npc_guid": npc_guid, "quest_id": window["quest_id"]},
                             f"accept quest {window.get('title') or window['quest_id']}"))
    elif kind in ("quest_list", "gossip") and npc_guid is not None:
        for q in window.get("quests") or []:
            title = q.get("title") or q["quest_id"]
            if q["quest_id"] in in_log:
                out.append(candidate(A.COMPLETE_QUEST, {"npc_guid": npc_guid, "quest_id": q["quest_id"]},
                                     f"hand in quest {title}"))
            else:
                out.append(candidate(A.ACCEPT_QUEST, {"npc_guid": npc_guid, "quest_id": q["quest_id"]},
                                     f"accept quest {title}"))
        if kind == "gossip":
            # Coded options need typed text; that's free text, so never offered.
            options = [o for o in window.get("options") or [] if not o.get("coded")]
            for o in options[:MAX_GOSSIP_OPTIONS]:
                out.append(candidate(A.GOSSIP_SELECT, {"option_index": o["index"]},
                                     f"choose dialogue option: {o.get('text') or o['index']}"))
    elif kind == "quest_request_items" and npc_guid is not None:
        out.append(candidate(A.COMPLETE_QUEST, {"npc_guid": npc_guid, "quest_id": window["quest_id"]},
                             f"hand in quest {window.get('title') or window['quest_id']}"))
    elif kind == "quest_offer_reward" and npc_guid is not None:
        title = window.get("title") or window["quest_id"]
        choices = window.get("reward_choice_items") or []
        for i, item in enumerate(choices or [None]):
            what = f" taking reward item {item.get('entry')}" if item else ""
            out.append(candidate(A.TURN_IN_QUEST,
                                 {"npc_guid": npc_guid, "quest_id": window["quest_id"], "reward_choice": i},
                                 f"turn in quest {title}{what}"))

    out.append(candidate(A.CLOSE_WINDOW, {}, f"close the {kind or 'open'} window"))
    return out


def generate(snapshot: dict, *, my_guid: int | None = None, reflex_state: dict | None = None,
             history: list | None = None,
             limit: int = MAX_CANDIDATES, handles=None,
             notes: list | None = None) -> list[dict]:
    """Concrete candidates for this think cycle; see the module docstring.
    `handles` (world.handles) resolves handle-string GUIDs (UM-89) for the
    unit comparisons; without it only raw-int snapshots are understood.
    `history` (ThinkState.for_prompt()) drops/demotes options the agent just
    failed or repeated to no effect; `notes`, if given, collects what was
    changed and why."""
    limit = max(1, limit)
    idle = candidate(A.IDLE, {}, "do nothing this cycle")
    out = []

    # Dead: release, then reclaim. No reflex does either (gh-208), and
    # corpse_position may be unknown, so reclaim_corpse is always offered to a
    # ghost; it queries the server for the corpse itself.
    if snapshot.get("is_dead") or snapshot.get("is_ghost"):
        if snapshot.get("is_ghost"):
            out.append(candidate(A.RECLAIM_CORPSE, {}, "run back to your corpse and resurrect"))
            corpse = snapshot.get("corpse_position")
            if isinstance(corpse, dict):
                out.append(candidate(A.MOVE_TO, {"x": corpse["x"], "y": corpse["y"], "z": corpse["z"]},
                                     "run back to your corpse"))
        else:
            out.append(candidate(A.RELEASE_SPIRIT, {}, "release your spirit to become a ghost"))
        return _finish(out, idle, limit)

    out.extend(_window_candidates(snapshot))

    # Known, metadata-backed spells only; only combat threats, and pass their
    # snapshot handle through unchanged for UM-89 resolution at execution.
    threats_snapshot = [u for u in snapshot.get("nearby_units") or []
                        if _alive(u) and u.get("in_combat") and my_guid is not None
                        and _guid_value(u, "target_guid", handles) == my_guid]
    # Damaging spells are offered against threats below; prefer the highest
    # known rank for a spell family. Heals are offered separately (self-cast,
    # when health is low); buffs and utility spells are still not offered.
    offensive_ids = {20271: "judgement", 2812: "holy_wrath", 2973: "raptor",
                     1978: "serpent_sting", 1752: "sinister_strike", 2098: "eviscerate",
                     585: "smite", 589: "shadow_word_pain", 133: "fireball",
                     116: "frostbolt", 143: "fireball"}
    rank = {133: 1, 143: 2}
    spell_by_family = {}
    for spell in snapshot.get("spells") or []:
        family = offensive_ids.get(spell.get("id"))
        if family and (family not in spell_by_family or rank.get(spell["id"], 1) >
                       rank.get(spell_by_family[family]["id"], 1)):
            spell_by_family[family] = spell
    for spell in list(spell_by_family.values())[:MAX_SPELLS]:
        if threats_snapshot:
            out.append(candidate(A.CAST_SPELL, {"spell_id": spell["id"],
                                                  "target_guid": threats_snapshot[0]["guid"]},
                                 f"cast {spell.get('name') or spell['id']} at {_name(threats_snapshot[0])}"))

    # Offer on-use consumables only when a health or mana resource is low.
    # Quest items aren't consumables and remain available to quest actions.
    me = snapshot.get("me") or {}
    health_pct = _resource_pct(me, "health")
    mana_pct = _resource_pct(me, "mana")
    needs_health, needs_mana = health_pct is not None and health_pct < 0.7, mana_pct is not None and mana_pct < 0.5
    # Heals are offered as a self-cast (no target) when health is low, so a
    # character whose only known spell is a heal (level-1 Paladin: Holy Light)
    # can still cast and save itself.
    if needs_health:
        known_heals = [s for s in snapshot.get("spells") or [] if s.get("id") in HEAL_SPELL_IDS]
        for spell in known_heals[:MAX_SPELLS]:
            out.insert(0, candidate(A.CAST_SPELL, {"spell_id": spell["id"]},
                                    f"cast {spell.get('name') or spell['id']} on yourself"))

    use_count = 0
    for item in sorted((snapshot.get("inventory") or []),
                       key=lambda it: (-it.get("template", {}).get("quality", 0), it.get("slot", 999))):
        template = item.get("template") or {}
        spells = template.get("spells") or []
        usable = any(s.get("trigger") == 0 for s in spells)
        if usable and _backpack(item) and (needs_health or needs_mana):
            out.append(candidate(A.USE_ITEM, {"bag": 255, "slot": item["slot"]},
                                 f"use {item.get('name') or item.get('entry')}"))
            use_count += 1
            if use_count >= MAX_USE_ITEMS:
                break

    # Only offer an item if its cached template proves it scores above the
    # currently equipped item in the same slot and is usable by this class.
    class_id = me.get("class_id")
    equipment = snapshot.get("equipment") or {}
    for item in (snapshot.get("inventory") or []):
        tpl = item.get("template")
        if not tpl or class_id not in item_compare.CLASS_PRIMARY_STAT or not _backpack(item):
            continue
        equip_slot = tpl.get("inventory_type")
        slots = _EQUIP_SLOTS.get(equip_slot)
        if not slots:
            continue
        current_items = [equipment[s] for s in slots if s in equipment and equipment[s].get("template")]
        # Two-handed items replace the main/off-hand pair, so compare against
        # their combined value. For paired rings/trinkets/weapons, beat the
        # weaker occupied slot to avoid requiring two replacements at once.
        current_scores = [item_compare.score_item(e["template"], class_id) for e in current_items]
        current_score = sum(current_scores) if equip_slot == 17 else (min(current_scores) if current_scores else None)
        if (item_compare.usability_error(tpl, class_id) is None and
                (current_score is None or item_compare.score_item(tpl, class_id) > current_score)):
            out.append(candidate(A.EQUIP_ITEM, {"bag": 255, "slot": item["slot"]},
                                 f"equip upgrade {item.get('name') or item.get('entry')}"))
            if sum(c["action"] == A.EQUIP_ITEM for c in out) >= MAX_EQUIP:
                break

    invite = snapshot.get("pending_invite")
    if invite:  # session.pending_invite: {"inviter_name": str}
        inviter = invite.get("inviter_name") if isinstance(invite, dict) else None
        out.append(candidate(A.ACCEPT_GROUP, {}, f"accept the group invite{' from ' + inviter if inviter else ''}"))

    units = sorted((u for u in snapshot.get("nearby_units") or []
                    if _guid_value(u, "guid", handles) is not None), key=_dist)
    my_level = _my_level(snapshot)

    threats = [u for u in units if _alive(u) and u.get("in_combat") and my_guid is not None
               and _guid_value(u, "target_guid", handles) == my_guid]
    for u in threats[:MAX_THREATS]:
        out.append(_engage(u, "attacking you"))
    threat_guids = {u["guid"] for u in threats}

    loot = [u for u in units if u.get("lootable") and _dist(u) <= APPROACH_MAX_YD]
    for u in loot[:MAX_LOOT]:
        if _dist(u) <= INTERACT_RANGE_YD:
            out.append(candidate(A.LOOT, {"guid": u["guid"]}, f"loot {_name(u)} ({_dist(u):.1f} yd)"))
        else:
            out.append(candidate(A.MOVE_TOWARDS, {"guid": u["guid"], "stop_distance": 2.0},
                                 f"move to loot {_name(u)} ({_dist(u):.1f} yd)"))

    givers = [u for u in units if u.get("quest_giver_status") in _QUEST_GIVER_WORTH_VISITING
              and _dist(u) <= APPROACH_MAX_YD]
    for u in givers[:MAX_QUEST_GIVERS]:
        why = _QUEST_GIVER_WORTH_VISITING[u["quest_giver_status"]]
        if _dist(u) <= INTERACT_RANGE_YD:
            out.append(candidate(A.INTERACT, {"guid": u["guid"]}, f"talk to {_name(u)} ({why})"))
        else:
            out.append(candidate(A.MOVE_TOWARDS, {"guid": u["guid"], "stop_distance": 3.0},
                                 f"walk to {_name(u)} ({why}, {_dist(u):.1f} yd)"))

    for q in [q for q in snapshot.get("quest_log") or [] if q.get("state_name") == "failed"][:MAX_ABANDON]:
        out.append(candidate(A.ABANDON_QUEST, {"slot": q["slot"]},
                             f"abandon failed quest {q.get('title') or q.get('quest_id')}"))

    targets = [u for u in units if u["guid"] not in threat_guids and _alive(u) and not u.get("lootable")
               and not u.get("npc_flags") and u.get("quest_giver_status") in (None, "none")
               and _dist(u) <= APPROACH_MAX_YD
               # unprovoked pulls only when both levels are known and the mob isn't too strong
               and my_level is not None and isinstance(u.get("level"), int)
               and u["level"] <= my_level + ATTACK_LEVEL_MARGIN
               # someone else's fight: not ours to take (only knowable with my_guid)
               and not (my_guid is not None and u.get("in_combat")
                        and _guid_value(u, "target_guid", handles) not in (None, 0, my_guid))]
    for u in targets[:MAX_ATTACK]:
        out.append(_engage(u, ""))

    follow = (reflex_state or {}).get("follow") or {}
    if follow.get("enabled"):
        leader = follow.get("leader_name") or "the leader"
        out.append(candidate(A.STOP_FOLLOWING, {}, f"stop following {leader}"))
        on = not follow.get("assist")
        out.append(candidate(A.ASSIST, {"on": on},
                             f"{'start' if on else 'stop'} assisting {leader}'s target"))
    else:
        players = sorted((p for p in snapshot.get("nearby_players") or [] if p.get("name")), key=_dist)
        for p in players[:MAX_FOLLOW]:
            out.append(candidate(A.FOLLOW, {"player_name": p["name"]},
                                 f"follow {p['name']} ({_dist(p):.1f} yd)"))

    drop, demote = _history_effects(history)
    # Params carry the snapshot's own guid values, as do threat_guids.
    protected = {c["id"] for c in out
                 if c["params"].get("guid") in threat_guids
                 or c["params"].get("target_guid") in threat_guids}
    return _finish(out, idle, limit, drop, demote, protected, notes)


HISTORY_DROP_WINDOW = 4     # only the most recent entries can drop an option
HISTORY_DEMOTE_REPEATS = 2  # consecutive no-effect successes before demotion
# Error text (agent/think.py, agent/handles.py) that retrying the same args cannot fix.
_DETERMINISTIC_ERRORS = ("missing required params", "bad params", "unknown handle", "unknown action")


def _history_effects(history) -> tuple[dict, dict]:
    """({id: reason} to drop, {id: reason} to demote) from ThinkState history.
    Tolerates malformed entries (skipped); never raises."""
    entries = []
    for h in history or []:
        if not isinstance(h, dict) or not isinstance(h.get("action"), str):
            continue
        args = h.get("args")
        args = args if isinstance(args, dict) else {}
        try:
            cid = candidate(h["action"], args, "")["id"]
        except Exception:
            continue
        entries.append((cid, h))
    drop, demote = {}, {}

    recent = entries[-HISTORY_DROP_WINDOW:]
    by_id = {}
    for cid, h in recent:
        by_id.setdefault(cid, []).append(h)
    for cid, hs in by_id.items():
        last = hs[-1]
        if last.get("ok") is not False:
            continue
        err = str(last.get("error") or "")
        if any(m in err.lower() for m in _DETERMINISTIC_ERRORS):
            drop[cid] = f"rejected: {err}"
        elif len(hs) >= 2 and hs[-2].get("ok") is False and hs[-2].get("error") == last.get("error"):
            drop[cid] = f"failed twice in a row: {err}"

    if entries:
        tail_id = entries[-1][0]
        run = 0
        for cid, h in reversed(entries):
            if cid != tail_id or h.get("ok") is not True or h.get("changed") is not False:
                break
            run += 1
        if run >= HISTORY_DEMOTE_REPEATS and tail_id not in drop:
            demote[tail_id] = f"repeated {run}x with no effect"
    return drop, demote


def _finish(out: list, idle: dict, limit: int, drop: dict | None = None,
            demote: dict | None = None, protected: set | frozenset = frozenset(),
            notes: list | None = None) -> list:
    """De-duplicate by id (first, highest-priority wins), apply the history
    `drop`/`demote` maps ({id: reason}; `protected` ids are exempt), cap, end
    with idle."""
    drop, demote = drop or {}, demote or {}
    seen, unique, demoted = set(), [], []
    for c in out:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        if c["id"] not in protected:
            if c["id"] in drop:
                if notes is not None:
                    notes.append({"id": c["id"], "effect": "dropped", "reason": drop[c["id"]]})
                continue
            if c["id"] in demote:
                if notes is not None:
                    notes.append({"id": c["id"], "effect": "demoted", "reason": demote[c["id"]]})
                demoted.append(c)
                continue
        unique.append(c)
    return (unique + demoted)[:limit - 1] + [idle]
