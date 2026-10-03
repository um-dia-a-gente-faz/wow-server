"""Candidate generator (UM-97): turn one perception snapshot into a short,
bounded list of concrete actions a choice-only brain (Jev, UM-95/UM-99) can
pick from.

The LLM path (agent/llm.py) hands the model the whole action catalog as
tools and lets it fill in arguments. Jev works the other way round: it only
picks one option from a list we supply and never writes arguments. So the
arguments are decided here, in code, from `world.snapshot()`.

Interface
---------

    generate(snapshot, *, my_guid=None, reflex_state=None, limit=MAX_CANDIDATES)
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

Candidates only reference actions that are already registered: the follow
reflex's `follow`/`assist`/`stop_following` (agent/reflexes/follow.py) and
`idle` (agent/actions.py). Each action's own `check()` still runs when the
chosen candidate is executed, so a stale candidate fails safely.
"""

MAX_CANDIDATES = 15

MELEE_RANGE_YD = 5.0      # agent.actions.MELEE_RANGE_YD (auto_attack's check)
INTERACT_RANGE_YD = 5.0   # agent.npc / agent.quests INTERACT_RANGE_YD, loot range too
APPROACH_MAX_YD = 40.0    # move_towards is straight-line only (no navmesh): short hops
ATTACK_LEVEL_MARGIN = 3   # don't offer to pull mobs more than this many levels above us

MAX_THREATS = 3
MAX_LOOT = 3
MAX_QUEST_GIVERS = 3
MAX_ATTACK = 3
MAX_FOLLOW = 2
MAX_GOSSIP_OPTIONS = 4
MAX_ABANDON = 2

# agent.quests.QUEST_GIVER_STATUS_NAMES values worth walking over for.
_QUEST_GIVER_WORTH_VISITING = {"available": "has a quest", "reward": "quest ready to turn in",
                               "reward_rep": "quest ready to turn in"}


def candidate(action: str, params: dict, label: str) -> dict:
    """Build one candidate with its stable id."""
    key = ",".join(f"{k}={params[k]}" for k in sorted(params))
    return {"id": f"{action}:{key}" if key else action, "label": label,
            "action": action, "params": dict(params)}


def _name(unit: dict) -> str:
    return unit.get("name") or f"unit {unit.get('guid', 0):#x}"


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
        return candidate("auto_attack", {"guid": unit["guid"]}, f"attack {desc}")
    return candidate("move_towards", {"guid": unit["guid"], "stop_distance": 3.0},
                     f"move to attack {desc}")


def _window_candidates(snapshot: dict) -> list:
    window = snapshot.get("window")
    if not isinstance(window, dict):
        return []
    kind = window.get("kind")
    npc_guid = window.get("npc_guid")
    in_log = {q.get("quest_id") for q in snapshot.get("quest_log") or []}
    out = []

    if kind == "quest_details" and npc_guid is not None:
        out.append(candidate("accept_quest", {"npc_guid": npc_guid, "quest_id": window["quest_id"]},
                             f"accept quest {window.get('title') or window['quest_id']}"))
    elif kind in ("quest_list", "gossip") and npc_guid is not None:
        for q in window.get("quests") or []:
            title = q.get("title") or q["quest_id"]
            if q["quest_id"] in in_log:
                out.append(candidate("complete_quest", {"npc_guid": npc_guid, "quest_id": q["quest_id"]},
                                     f"hand in quest {title}"))
            else:
                out.append(candidate("accept_quest", {"npc_guid": npc_guid, "quest_id": q["quest_id"]},
                                     f"accept quest {title}"))
        if kind == "gossip":
            # Coded options need typed text; that's free text, so never offered.
            options = [o for o in window.get("options") or [] if not o.get("coded")]
            for o in options[:MAX_GOSSIP_OPTIONS]:
                out.append(candidate("gossip_select", {"option_index": o["index"]},
                                     f"choose dialogue option: {o.get('text') or o['index']}"))
    elif kind == "quest_request_items" and npc_guid is not None:
        out.append(candidate("complete_quest", {"npc_guid": npc_guid, "quest_id": window["quest_id"]},
                             f"hand in quest {window.get('title') or window['quest_id']}"))
    elif kind == "quest_offer_reward" and npc_guid is not None:
        title = window.get("title") or window["quest_id"]
        choices = window.get("reward_choice_items") or []
        for i, item in enumerate(choices or [None]):
            what = f" taking reward item {item.get('entry')}" if item else ""
            out.append(candidate("turn_in_quest",
                                 {"npc_guid": npc_guid, "quest_id": window["quest_id"], "reward_choice": i},
                                 f"turn in quest {title}{what}"))

    out.append(candidate("close_window", {}, f"close the {kind or 'open'} window"))
    return out


def generate(snapshot: dict, *, my_guid: int | None = None, reflex_state: dict | None = None,
             limit: int = MAX_CANDIDATES) -> list[dict]:
    """Concrete candidates for this think cycle; see the module docstring."""
    limit = max(1, limit)
    idle = candidate("idle", {}, "do nothing this cycle")
    out = []

    # Dead: the death reflex releases/reclaims; the only useful move is the corpse run.
    if snapshot.get("is_dead") or snapshot.get("is_ghost"):
        corpse = snapshot.get("corpse_position")
        if snapshot.get("is_ghost") and isinstance(corpse, dict):
            out.append(candidate("move_to", {"x": corpse["x"], "y": corpse["y"], "z": corpse["z"]},
                                 "run back to your corpse"))
        return _finish(out, idle, limit)

    out.extend(_window_candidates(snapshot))

    invite = snapshot.get("pending_invite")
    if invite:  # session.pending_invite: {"inviter_name": str}
        inviter = invite.get("inviter_name") if isinstance(invite, dict) else None
        out.append(candidate("accept_group", {}, f"accept the group invite{' from ' + inviter if inviter else ''}"))

    units = sorted((u for u in snapshot.get("nearby_units") or [] if isinstance(u.get("guid"), int)),
                   key=_dist)
    my_level = _my_level(snapshot)

    threats = [u for u in units if _alive(u) and u.get("in_combat") and my_guid is not None
               and u.get("target_guid") == my_guid]
    for u in threats[:MAX_THREATS]:
        out.append(_engage(u, "attacking you"))
    threat_guids = {u["guid"] for u in threats}

    loot = [u for u in units if u.get("lootable") and _dist(u) <= APPROACH_MAX_YD]
    for u in loot[:MAX_LOOT]:
        if _dist(u) <= INTERACT_RANGE_YD:
            out.append(candidate("loot", {"guid": u["guid"]}, f"loot {_name(u)} ({_dist(u):.1f} yd)"))
        else:
            out.append(candidate("move_towards", {"guid": u["guid"], "stop_distance": 2.0},
                                 f"move to loot {_name(u)} ({_dist(u):.1f} yd)"))

    givers = [u for u in units if u.get("quest_giver_status") in _QUEST_GIVER_WORTH_VISITING
              and _dist(u) <= APPROACH_MAX_YD]
    for u in givers[:MAX_QUEST_GIVERS]:
        why = _QUEST_GIVER_WORTH_VISITING[u["quest_giver_status"]]
        if _dist(u) <= INTERACT_RANGE_YD:
            out.append(candidate("interact", {"guid": u["guid"]}, f"talk to {_name(u)} ({why})"))
        else:
            out.append(candidate("move_towards", {"guid": u["guid"], "stop_distance": 3.0},
                                 f"walk to {_name(u)} ({why}, {_dist(u):.1f} yd)"))

    for q in [q for q in snapshot.get("quest_log") or [] if q.get("state_name") == "failed"][:MAX_ABANDON]:
        out.append(candidate("abandon_quest", {"slot": q["slot"]},
                             f"abandon failed quest {q.get('title') or q.get('quest_id')}"))

    targets = [u for u in units if u["guid"] not in threat_guids and _alive(u) and not u.get("lootable")
               and not u.get("npc_flags") and u.get("quest_giver_status") in (None, "none")
               and _dist(u) <= APPROACH_MAX_YD
               # unprovoked pulls only when both levels are known and the mob isn't too strong
               and my_level is not None and isinstance(u.get("level"), int)
               and u["level"] <= my_level + ATTACK_LEVEL_MARGIN
               # someone else's fight: not ours to take (only knowable with my_guid)
               and not (my_guid is not None and u.get("in_combat")
                        and u.get("target_guid") not in (None, 0, my_guid))]
    for u in targets[:MAX_ATTACK]:
        out.append(_engage(u, ""))

    follow = (reflex_state or {}).get("follow") or {}
    if follow.get("enabled"):
        leader = follow.get("leader_name") or "the leader"
        out.append(candidate("stop_following", {}, f"stop following {leader}"))
        on = not follow.get("assist")
        out.append(candidate("assist", {"on": on},
                             f"{'start' if on else 'stop'} assisting {leader}'s target"))
    else:
        players = sorted((p for p in snapshot.get("nearby_players") or [] if p.get("name")), key=_dist)
        for p in players[:MAX_FOLLOW]:
            out.append(candidate("follow", {"player_name": p["name"]},
                                 f"follow {p['name']} ({_dist(p):.1f} yd)"))

    return _finish(out, idle, limit)


def _finish(out: list, idle: dict, limit: int) -> list:
    """De-duplicate by id (first, highest-priority wins), cap, end with idle."""
    seen, unique = set(), []
    for c in out:
        if c["id"] not in seen:
            seen.add(c["id"])
            unique.append(c)
    return unique[:limit - 1] + [idle]
