"""Game-rule constants: properties of the game, not of the deployment (those live in
config.SETTINGS). Pure values; this module imports nothing from agent/. What the LLM is
offered (candidates) and what is allowed (actions) read the same constant from here."""

# Interaction and melee reach, in yards. TrinityCore: Player::GetNPCIfCanInteractWith /
# GetGameObjectIfCanInteractWith use INTERACTION_DISTANCE (5.0); melee reach is ~ weapon
# range + average combat reach. Loot range matches interaction range.
INTERACT_RANGE_YD = 5.0
MELEE_RANGE_YD = 5.0
LOOT_RANGE_YD = 5.0

# Ghosts see much further than 50 yd in the real client; generous on purpose.
SPIRIT_HEALER_SEARCH_RANGE_YD = 200.0

# move_towards is straight-line only (no navmesh): candidates offer short hops only.
APPROACH_MAX_YD = 40.0

# Candidates do not offer to pull mobs more than this many levels above us.
ATTACK_LEVEL_MARGIN = 3
