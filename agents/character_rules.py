"""What a new agent character may be: the Horde race and class table and the name rules (#391).

Pure and stdlib-only. scripts/create_agent_roster.py plans the roster with it and
tools/agent-runner validates a create request with it. Verified against TrinityCore 3.3.5a
CharacterHandler.cpp: a Death Knight needs an existing level 55+ character on the account
(CONFIG_CHARACTER_CREATING_MIN_LEVEL_FOR_DEATH_KNIGHT), which a fresh AGENTnn account never
has, so class 6 is in no list here.
"""
import random


# Horde races (SharedDefines.h Races) -> the classes a fresh account can create.
# Death Knight (6) is left out on purpose, see the module docstring.
WARRIOR, PALADIN, HUNTER, ROGUE, PRIEST, SHAMAN, MAGE, WARLOCK, DRUID = 1, 2, 3, 4, 5, 7, 8, 9, 11
HORDE = {
    2:  ("Orc",       [WARRIOR, HUNTER, ROGUE, SHAMAN, WARLOCK]),
    5:  ("Undead",    [WARRIOR, ROGUE, PRIEST, MAGE, WARLOCK]),
    6:  ("Tauren",    [WARRIOR, HUNTER, SHAMAN, DRUID]),
    8:  ("Troll",     [WARRIOR, HUNTER, ROGUE, PRIEST, SHAMAN, MAGE]),
    10: ("Blood Elf", [WARRIOR, PALADIN, HUNTER, ROGUE, PRIEST, MAGE, WARLOCK]),
}
CLASS_NAMES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
               7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}

# Syllables per race, so names read like the race: "Grokhar", "Mulgore-ish", "Sylvaris".
_SYLLABLES = {
    2:  (["Gar", "Gro", "Thok", "Dra", "Kar", "Mog", "Ur", "Zug", "Bor"], ["hak", "gash", "mar", "nak", "thar", "dum", "gor"]),
    5:  (["Mor", "Vel", "Sar", "Ner", "Thal", "Ves", "Dal", "Cor"], ["wyn", "vis", "ric", "den", "gar", "lock", "mund"]),
    6:  (["Ba", "Ru", "Ma", "Ke", "Ta", "Hu", "Pa", "Mu"], ["hotah", "kuna", "wak", "hawk", "toka", "nuk", "rana"]),
    8:  (["Zul", "Jin", "Vol", "Rok", "Zan", "Sen", "Mah", "Tiki"], ["jin", "tar", "ja", "kazi", "ari", "ton", "rik"]),
    10: (["Sil", "Quel", "Aer", "Thal", "Lor", "Kael", "Anar", "Vel"], ["varis", "dorei", "ian", "anar", "eth", "thas", "lian"]),
}


def valid_name(name: str) -> bool:
    """2-12 ASCII letters, no letter three times in a row (TC's name rules)."""
    if not (2 <= len(name) <= 12) or not name.isascii() or not name.isalpha():
        return False
    lower = name.lower()
    return not any(lower[i] == lower[i + 1] == lower[i + 2] for i in range(len(lower) - 2))


def gen_name(rng: random.Random, race: int, taken: set[str]) -> str:
    pre, post = _SYLLABLES[race]
    for _ in range(500):
        name = rng.choice(pre) + rng.choice(post)
        if rng.random() < 0.3:
            name = rng.choice(pre) + rng.choice(pre).lower() + rng.choice(post)
        if valid_name(name) and name.lower() not in taken:
            return name
    raise RuntimeError(f"could not generate a free name for race {race}")
