#!/usr/bin/env python3
"""Would a Jev decision cache pay off? (#167, docs/adr/0003-jev-decision-cache.md)

Usage: python3 -m agent.tools.cache_probe <audit.jsonl|dir> [...] [--json]

Measurement only, nothing is cached. For every audit record that carries a
full snapshot (agent.audit writes one every FULL_SNAPSHOT_EVERY'th cycle and
on failures), rebuild the candidate list with agent.candidates.generate(),
compute the situation key from the ADR, and report:

  repeat rate   share of keyed cycles whose key was seen earlier in the run
  agreement     of those repeats, how often the earlier cycle's chosen option
                (normalised, so a different GUID of the same mob still counts)
                equals this cycle's
  refused       share of cycles in a class the ADR refuses to serve

Records are keyed by `situation_key` when the audit record has one, so a
future per-cycle audit field makes the numbers exact instead of sampled.
stdlib only.
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter

from agent import candidates as cand
from agent.tools.replay import load_records

# Health/mana bands: the edges are where the "obvious" behaviour changes
# (rest, heal, flee), not arbitrary buckets.
BAND_EDGES = (0.25, 0.5, 0.8)
BAND_NAMES = ("crit", "low", "mid", "full")
DIST_EDGES = (5.0, 15.0, 40.0)  # melee / interact, short hop, approach limit
THREAT_CAP = 3

# Action classes the ADR is willing to serve from cache; everything else is refused.
SERVED_ACTIONS = {"accept_quest", "complete_quest", "turn_in_quest", "loot", "accept_group",
                  "close_window", "idle", "stop_following"}
# Served only when the state says the obvious thing: resting is `idle` while
# out of combat and low; listed here for the refusal rule below.
RESTING_ACTIONS = {"idle"}


def _frac(text) -> float | None:
    """'40/90' -> 0.444; None when absent or unparsable."""
    try:
        cur, top = str(text).split("/")
        return int(cur) / int(top) if int(top) else None
    except (ValueError, TypeError):
        return None


def band(value: float | None) -> str:
    if value is None:
        return "?"
    for edge, name in zip(BAND_EDGES, BAND_NAMES):
        if value < edge:
            return name
    return BAND_NAMES[-1]


def _dist_band(d) -> int:
    if not isinstance(d, (int, float)):
        return len(DIST_EDGES)
    for i, edge in enumerate(DIST_EDGES):
        if d <= edge:
            return i
    return len(DIST_EDGES)


def normalise(c: dict, snapshot: dict) -> str:
    """The candidate with per-spawn identity removed: a unit GUID/handle
    becomes `entry@distance-band`, so "attack the wyrm at 3 yd" is the same
    option on two different wyrms. Everything else stays verbatim."""
    units = {str(u.get("guid")): u for u in snapshot.get("nearby_units") or []}
    parts = []
    for k in sorted(c.get("params") or {}):
        v = c["params"][k]
        if k in ("guid", "npc_guid") and str(v) in units:
            u = units[str(v)]
            v = f"{u.get('entry', u.get('name'))}@{_dist_band(u.get('distance'))}"
        parts.append(f"{k}={v}")
    return f"{c['action']}({','.join(parts)})"


def situation_key(snapshot: dict, candidates: list[dict]) -> str:
    """Key = salient state bands + the normalised, order-independent option set."""
    me = snapshot.get("me") or {}
    window = snapshot.get("window") if isinstance(snapshot.get("window"), dict) else {}
    threats = sum(1 for c in candidates if c["action"] in ("auto_attack", "move_towards")
                  and "attacking you" in c.get("label", ""))
    quests = sorted((q.get("quest_id"), q.get("state_name")) for q in snapshot.get("quest_log") or [])
    state = {
        "hp": band(_frac(me.get("health"))),
        "mana": band(_frac(me.get("mana"))),
        "combat": min(threats, THREAT_CAP),
        "window": window.get("kind"),
        "dead": bool(snapshot.get("is_dead") or snapshot.get("is_ghost")),
        "quests": quests,
    }
    options = sorted({normalise(c, snapshot) for c in candidates})
    return json.dumps({"state": state, "options": options}, sort_keys=True, default=str)


def refused(action: str | None, key: str) -> bool:
    """True when the ADR forbids serving this decision from cache."""
    if action not in SERVED_ACTIONS:
        return True
    if action in RESTING_ACTIONS:
        state = json.loads(key)["state"]
        return not (state["combat"] == 0 and (state["hp"] in ("crit", "low") or state["mana"] in ("crit", "low")))
    return False


def analyse(records) -> dict:
    seen: dict[str, str] = {}
    n = repeats = same = refused_n = 0
    classes: Counter = Counter()
    for rec in records:
        action = (rec.get("tool_call") or {}).get("name")
        key = rec.get("situation_key")
        norm = rec.get("chosen_normalised")
        snap = rec.get("snapshot")
        if key is None and isinstance(snap, dict):
            cands = cand.generate(snap, my_guid=(snap.get("me") or {}).get("guid"))
            key = situation_key(snap, cands)
            chosen = [c for c in cands if c["action"] == action
                      and c["params"] == (rec["tool_call"].get("args") or {})]
            norm = normalise(chosen[0], snap) if chosen else action
        if key is None:
            continue
        n += 1
        classes[action] += 1
        if refused(action, key):
            refused_n += 1
        if key in seen:
            repeats += 1
            same += seen[key] == norm
        seen[key] = norm
    return {"keyed_cycles": n, "distinct_keys": len(seen), "repeats": repeats,
            "repeat_rate": repeats / n if n else None,
            "same_choice": same, "agreement": same / repeats if repeats else None,
            "refused": refused_n, "refused_rate": refused_n / n if n else None,
            "by_action": dict(classes)}


def _paths(args) -> list[str]:
    out = []
    for a in args:
        out += sorted(glob.glob(os.path.join(a, "**", "*.jsonl*"), recursive=True)) \
            if os.path.isdir(a) else [a]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    records = (r for p in _paths(args.paths) for r in load_records(p))
    stats = analyse(records)
    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        for k, v in stats.items():
            print(f"{k}: {round(v, 3) if isinstance(v, float) else v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
