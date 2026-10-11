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
levels above the agent (none at all while its level is unknown; while hurt
(needs_health) only units already in combat). Anything out of reach becomes a short `move_towards` hop.

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

from collections.abc import Callable
from typing import Literal

from .handles import UnknownHandle
from . import action_names as A, item_compare
from .action_specs import cap_of
from .candidate_history import finish, history_effects
from .candidate_history import HISTORY_DEMOTE_REPEATS, HISTORY_DROP_WINDOW  # noqa: F401 (re-exported)
from .model import ActionRecord, Candidate, Item, KnownSpell, Me, Snapshot, Unit
from .rules import APPROACH_MAX_YD, ATTACK_LEVEL_MARGIN, INTERACT_RANGE_YD, MELEE_RANGE_YD

MAX_CANDIDATES = 15

MAX_THREATS = 3
MAX_LOOT = cap_of(A.LOOT)
MAX_QUEST_GIVERS = cap_of(A.INTERACT)
MAX_ATTACK = 3
MAX_FOLLOW = cap_of(A.FOLLOW)
MAX_GOSSIP_OPTIONS = cap_of(A.GOSSIP_SELECT)
MAX_ABANDON = cap_of(A.ABANDON_QUEST)
MAX_SPELLS = cap_of(A.CAST_SPELL)
# Healing spells (agent.spells.SPELL_TABLE: Holy Light, Lesser Heal), self-cast when hurt.
HEAL_SPELL_IDS = frozenset({635, 2050})
MAX_TRAIN = cap_of(A.TRAIN_SPELL)
MAX_EQUIP = cap_of(A.EQUIP_ITEM)
MAX_USE_ITEMS = cap_of(A.USE_ITEM)
MAX_SELL = cap_of(A.SELL_ITEM)

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


def _backpack(item: Item) -> bool:
    return isinstance(item.get("slot"), int) and _BACKPACK_FIRST <= item["slot"] <= _BACKPACK_LAST


def _resource_pct(me: Me | None, resource: Literal["health", "mana"]) -> float | None:
    value = me.get(resource) if me else None
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
    **dict.fromkeys((A.OPEN_TRADE, A.ACCEPT_TRADE_REQUEST, A.OFFER_ITEM, A.OFFER_GOLD, A.ACCEPT_TRADE,
                     A.CANCEL_TRADE), "trade family is out of scope"),
    **dict.fromkeys((A.OPEN_MAILBOX, A.SEND_MAIL, A.TAKE_MAIL, A.DELETE_MAIL), "mail family is out of scope"),
}

# agent.quests.QUEST_GIVER_STATUS_NAMES values worth walking over for.
_QUEST_GIVER_WORTH_VISITING = {"available": "has a quest", "reward": "quest ready to turn in",
                               "reward_rep": "quest ready to turn in"}


def candidate(action: str, params: dict, label: str) -> Candidate:
    """Build one candidate with its stable id."""
    key = ",".join(f"{k}={params[k]}" for k in sorted(params))
    return {"id": f"{action}:{key}" if key else action, "label": label,
            "action": action, "params": dict(params)}


def _name(unit: Unit) -> str:
    name = unit.get("name")
    if name:
        return name
    guid = unit.get("guid")
    # guid may be a handle string ("u3") on an encoded snapshot (UM-89).
    return f"unit {guid:#x}" if isinstance(guid, int) else f"unit {guid or 'unknown'}"


def _guid_value(unit: Unit, key: Literal["guid", "target_guid"], handles) -> int | None:
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


def _dist(unit: Unit) -> float:
    d = unit.get("distance")
    return d if isinstance(d, (int, float)) else float("inf")


def _alive(unit: Unit) -> bool:
    hp = unit.get("health_pct")
    return hp is None or hp > 0


def _my_level(snapshot: Snapshot):
    me = snapshot.get("me")
    level = me.get("level") if me else None
    return level if isinstance(level, int) else None


def _engage(unit: Unit, why: str) -> Candidate:
    """auto_attack when in melee range, else a short approach."""
    desc = f"{_name(unit)} (level {unit.get('level', '?')}, {_dist(unit):.1f} yd{', ' + why if why else ''})"
    if _dist(unit) <= MELEE_RANGE_YD:
        return candidate(A.AUTO_ATTACK, {"guid": unit["guid"]}, f"attack {desc}")
    return candidate(A.MOVE_TOWARDS, {"guid": unit["guid"], "stop_distance": 3.0},
                     f"move to attack {desc}")


def _window_candidates(snapshot: Snapshot) -> list[Candidate]:
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


class _Context:
    """What every provider reads, derived once per call and never mutated; a dead snapshot gets only plain fields."""

    def __init__(self, snapshot: Snapshot, my_guid: int | None, reflex_state: dict | None, handles) -> None:
        self.snapshot = snapshot
        self.my_guid = my_guid
        self.reflex_state = reflex_state
        self.handles = handles
        self.dead = bool(snapshot.get("is_dead") or snapshot.get("is_ghost"))
        if self.dead:
            return
        self.me = snapshot.get("me")
        self.in_combat = bool(self.me and self.me.get("in_combat"))  # gh-544: absent means out of combat
        health_pct, mana_pct = _resource_pct(self.me, "health"), _resource_pct(self.me, "mana")
        self.needs_health = health_pct is not None and health_pct < 0.7
        self.needs_mana = mana_pct is not None and mana_pct < 0.5
        # Combat threats in snapshot order (for spells); handles pass through unchanged (UM-89).
        self.spell_threats = [u for u in snapshot.get("nearby_units") or [] if self._attacks_me(u)]
        self.units = sorted((u for u in snapshot.get("nearby_units") or []
                             if _guid_value(u, "guid", handles) is not None), key=_dist)
        self.my_level = _my_level(snapshot)
        self.threats = [u for u in self.units if self._attacks_me(u)]
        self.threat_guids = {u["guid"] for u in self.threats}

    def _attacks_me(self, unit: Unit) -> bool:
        return bool(_alive(unit) and unit.get("in_combat") and self.my_guid is not None
                    and _guid_value(unit, "target_guid", self.handles) == self.my_guid)


Provider = Callable[[_Context, list[Candidate]], None]


def offers(*actions: str) -> Callable[[Provider], Provider]:
    """Tag a provider with the action names it can emit (a test checks each has an ActionSpec)."""
    def mark(provider: Provider) -> Provider:
        provider.actions = actions  # type: ignore[attr-defined]
        return provider
    return mark


@offers(A.RELEASE_SPIRIT, A.RECLAIM_CORPSE, A.MOVE_TO)
def _dead_candidates(ctx: _Context, out: list[Candidate]) -> None:
    # Release, then reclaim (no reflex does either, gh-208). corpse_position may be unknown, so a
    # ghost is always offered reclaim_corpse; it queries the server for the corpse itself.
    if ctx.snapshot.get("is_ghost"):
        out.append(candidate(A.RECLAIM_CORPSE, {}, "run back to your corpse and resurrect"))
        corpse = ctx.snapshot.get("corpse_position")
        if isinstance(corpse, dict):
            out.append(candidate(A.MOVE_TO, {"x": corpse["x"], "y": corpse["y"], "z": corpse["z"]},
                                 "run back to your corpse"))
    else:
        out.append(candidate(A.RELEASE_SPIRIT, {}, "release your spirit to become a ghost"))


@offers(A.TRAIN_SPELL, A.SELL_ITEM, A.ACCEPT_QUEST, A.COMPLETE_QUEST, A.GOSSIP_SELECT, A.TURN_IN_QUEST,
        A.CLOSE_WINDOW)
def _window_provider(ctx: _Context, out: list[Candidate]) -> None:
    out.extend(_window_candidates(ctx.snapshot))


# Damaging spells only (highest known rank per family); buffs and utility spells are not offered.
_OFFENSIVE_SPELL_FAMILY = {20271: "judgement", 2812: "holy_wrath", 2973: "raptor",
                           1978: "serpent_sting", 1752: "sinister_strike", 2098: "eviscerate",
                           585: "smite", 589: "shadow_word_pain", 133: "fireball",
                           116: "frostbolt", 143: "fireball"}
_SPELL_RANK = {133: 1, 143: 2}


@offers(A.CAST_SPELL)
def _offensive_spell_provider(ctx: _Context, out: list[Candidate]) -> None:
    spell_by_family: dict[str, KnownSpell] = {}
    for spell in ctx.snapshot.get("spells") or []:
        family = _OFFENSIVE_SPELL_FAMILY.get(spell.get("id", 0))
        if family and (family not in spell_by_family or _SPELL_RANK.get(spell["id"], 1) >
                       _SPELL_RANK.get(spell_by_family[family]["id"], 1)):
            spell_by_family[family] = spell
    for spell in list(spell_by_family.values())[:MAX_SPELLS]:
        if ctx.spell_threats:
            out.append(candidate(A.CAST_SPELL, {"spell_id": spell["id"],
                                                  "target_guid": ctx.spell_threats[0]["guid"]},
                                 f"cast {spell.get('name') or spell['id']} at {_name(ctx.spell_threats[0])}"))


@offers(A.CAST_SPELL)
def _heal_provider(ctx: _Context, out: list[Candidate]) -> None:
    # A self-cast (no target) when health is low, so a character whose only known spell is a heal
    # (level-1 Paladin: Holy Light) can still cast and save itself. Heals go in front of everything
    # offered so far.
    if ctx.needs_health:
        known_heals = [s for s in ctx.snapshot.get("spells") or [] if s.get("id") in HEAL_SPELL_IDS]
        for spell in known_heals[:MAX_SPELLS]:
            out.insert(0, candidate(A.CAST_SPELL, {"spell_id": spell["id"]},
                                    f"cast {spell.get('name') or spell['id']} on yourself"))


@offers(A.USE_ITEM)
def _consumable_provider(ctx: _Context, out: list[Candidate]) -> None:
    # On-use consumables only when a health or mana resource is low. Quest items aren't
    # consumables and remain available to quest actions.
    use_count = 0
    for item in sorted((ctx.snapshot.get("inventory") or []),
                       key=lambda it: (-it.get("template", {}).get("quality", 0), it.get("slot", 999))):
        template = item.get("template") or {}
        spells = template.get("spells") or []
        usable = any(s.get("trigger") == 0 for s in spells)
        if usable and _backpack(item) and (ctx.needs_health or ctx.needs_mana):
            out.append(candidate(A.USE_ITEM, {"bag": 255, "slot": item["slot"]},
                                 f"use {item.get('name') or item.get('entry')}"))
            use_count += 1
            if use_count >= MAX_USE_ITEMS:
                break


@offers(A.EQUIP_ITEM)
def _equip_provider(ctx: _Context, out: list[Candidate]) -> None:
    # Only an item whose cached template proves it scores above the currently equipped item in the
    # same slot and is usable by this class.
    class_id = ctx.me.get("class_id") if ctx.me else None
    equipment = ctx.snapshot.get("equipment") or {}
    for item in (ctx.snapshot.get("inventory") or []):
        tpl = item.get("template")
        if not tpl or class_id not in item_compare.CLASS_PRIMARY_STAT or not _backpack(item):
            continue
        equip_slot = tpl.get("inventory_type", 0)
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


@offers(A.ACCEPT_GROUP)
def _group_invite_provider(ctx: _Context, out: list[Candidate]) -> None:
    invite = ctx.snapshot.get("pending_invite")
    if invite:  # session.pending_invite: {"inviter_name": str}
        inviter = invite.get("inviter_name") if isinstance(invite, dict) else None
        out.append(candidate(A.ACCEPT_GROUP, {}, f"accept the group invite{' from ' + inviter if inviter else ''}"))


@offers(A.AUTO_ATTACK, A.MOVE_TOWARDS)
def _threat_provider(ctx: _Context, out: list[Candidate]) -> None:
    for u in ctx.threats[:MAX_THREATS]:
        out.append(_engage(u, "attacking you"))


@offers(A.LOOT, A.MOVE_TOWARDS)
def _loot_provider(ctx: _Context, out: list[Candidate]) -> None:
    loot = [u for u in ctx.units if u.get("lootable") and _dist(u) <= APPROACH_MAX_YD]
    for u in loot[:MAX_LOOT]:
        if _dist(u) <= INTERACT_RANGE_YD:
            out.append(candidate(A.LOOT, {"guid": u["guid"]}, f"loot {_name(u)} ({_dist(u):.1f} yd)"))
        else:
            out.append(candidate(A.MOVE_TOWARDS, {"guid": u["guid"], "stop_distance": 2.0},
                                 f"move to loot {_name(u)} ({_dist(u):.1f} yd)"))


@offers(A.INTERACT, A.MOVE_TOWARDS)
def _quest_giver_provider(ctx: _Context, out: list[Candidate]) -> None:
    givers = [u for u in ctx.units if u.get("quest_giver_status") in _QUEST_GIVER_WORTH_VISITING
              and _dist(u) <= APPROACH_MAX_YD]
    for u in givers[:MAX_QUEST_GIVERS]:
        why = _QUEST_GIVER_WORTH_VISITING[str(u["quest_giver_status"])]
        if _dist(u) <= INTERACT_RANGE_YD:
            out.append(candidate(A.INTERACT, {"guid": u["guid"]}, f"talk to {_name(u)} ({why})"))
        else:
            out.append(candidate(A.MOVE_TOWARDS, {"guid": u["guid"], "stop_distance": 3.0},
                                 f"walk to {_name(u)} ({why}, {_dist(u):.1f} yd)"))


@offers(A.ABANDON_QUEST)
def _abandon_quest_provider(ctx: _Context, out: list[Candidate]) -> None:
    for q in [q for q in ctx.snapshot.get("quest_log") or [] if q.get("state_name") == "failed"][:MAX_ABANDON]:
        out.append(candidate(A.ABANDON_QUEST, {"slot": q["slot"]},
                             f"abandon failed quest {q.get('title') or q.get('quest_id')}"))


@offers(A.AUTO_ATTACK, A.MOVE_TOWARDS)
def _attack_provider(ctx: _Context, out: list[Candidate]) -> None:
    targets = [u for u in ctx.units if u["guid"] not in ctx.threat_guids and _alive(u) and not u.get("lootable")
               and not u.get("npc_flags") and u.get("quest_giver_status") in (None, "none") and _dist(u) <= APPROACH_MAX_YD
               # hurt: only a unit already fighting (health does not regen in combat, so a stunned attacker stays),
               # unless we are in combat with no threat: a stuck combat flag, so the pull is offered again (gh-544)
               and not (ctx.needs_health and not u.get("in_combat") and not (ctx.in_combat and not ctx.threats))
               # unprovoked pulls only when both levels are known and the mob isn't too strong
               and ctx.my_level is not None and isinstance(u.get("level"), int)
               and u["level"] <= ctx.my_level + ATTACK_LEVEL_MARGIN
               # someone else's fight: not ours to take (only knowable with my_guid)
               and not (ctx.my_guid is not None and u.get("in_combat")
                        and _guid_value(u, "target_guid", ctx.handles) not in (None, 0, ctx.my_guid))]
    for u in targets[:MAX_ATTACK]:
        out.append(_engage(u, ""))


# follow/assist/stop_following are driven by the follow reflex's state, not by the snapshot; they
# stay together behind this one provider until their specs carry it (gh-515).
@offers(A.FOLLOW, A.ASSIST, A.STOP_FOLLOWING)
def _follow_reflex_provider(ctx: _Context, out: list[Candidate]) -> None:
    follow = (ctx.reflex_state or {}).get("follow") or {}
    if follow.get("enabled"):
        leader = follow.get("leader_name") or "the leader"
        out.append(candidate(A.STOP_FOLLOWING, {}, f"stop following {leader}"))
        on = not follow.get("assist")
        out.append(candidate(A.ASSIST, {"on": on},
                             f"{'start' if on else 'stop'} assisting {leader}'s target"))
    else:
        players = sorted((p for p in ctx.snapshot.get("nearby_players") or [] if p.get("name")), key=_dist)
        for p in players[:MAX_FOLLOW]:
            out.append(candidate(A.FOLLOW, {"player_name": p["name"]},
                                 f"follow {p['name']} ({_dist(p):.1f} yd)"))


# Priority order: a provider's candidates go after the earlier providers' (heals are the one
# exception, they jump the queue themselves). Caps are counted over the shared list.
ALIVE_PROVIDERS: tuple[Provider, ...] = (
    _window_provider, _offensive_spell_provider, _heal_provider, _consumable_provider, _equip_provider,
    _group_invite_provider, _threat_provider, _loot_provider, _quest_giver_provider,
    _abandon_quest_provider, _attack_provider, _follow_reflex_provider,
)
DEAD_PROVIDERS: tuple[Provider, ...] = (_dead_candidates,)


def generate(snapshot: Snapshot, *, my_guid: int | None = None, reflex_state: dict | None = None,
             history: list[ActionRecord] | None = None,
             limit: int = MAX_CANDIDATES, handles=None,
             notes: list | None = None) -> list[Candidate]:
    """Concrete candidates for this think cycle; see the module docstring.
    `handles` (world.handles) resolves handle-string GUIDs (UM-89) for the
    unit comparisons; without it only raw-int snapshots are understood.
    `history` (ThinkState.for_prompt()) drops/demotes options the agent just
    failed or repeated to no effect; `notes`, if given, collects what was
    changed and why."""
    limit = max(1, limit)
    idle = candidate(A.IDLE, {}, "do nothing this cycle")
    ctx = _Context(snapshot, my_guid, reflex_state, handles)
    out: list[Candidate] = []
    for provider in DEAD_PROVIDERS if ctx.dead else ALIVE_PROVIDERS:
        provider(ctx, out)
    if ctx.dead:  # history is about the living options
        return finish(out, idle, limit)

    drop, demote = history_effects(history, candidate)
    # Params carry the snapshot's own guid values, as do threat_guids.
    protected = {c["id"] for c in out
                 if c["params"].get("guid") in ctx.threat_guids
                 or c["params"].get("target_guid") in ctx.threat_guids}
    return finish(out, idle, limit, drop, demote, protected, notes)
