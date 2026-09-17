#!/usr/bin/env python3
"""Minimal tool schema catalog for the benchmark, standing in for UM-36/UM-44's
real action registry (not built yet at spike time). Shaped after the action
catalog in docs/AI-AGENT-SPEC.md so tool-call validation in benchmark.py
exercises realistic argument schemas.

Kept deliberately small (no C++/DB round-trip) — the point is to measure
whether a model calls exactly one tool from this list with valid arguments,
the same shape UM-44's real catalog will present.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "move_to",
            "description": "Pathfind and walk to a position.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "z": {"type": "number"},
                },
                "required": ["x", "y", "z"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_towards",
            "description": "Move towards a unit or object by GUID.",
            "parameters": {
                "type": "object",
                "properties": {"guid": {"type": "integer"}},
                "required": ["guid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_target",
            "description": "Select a target by GUID.",
            "parameters": {
                "type": "object",
                "properties": {"guid": {"type": "integer"}},
                "required": ["guid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cast_spell",
            "description": "Cast a spell, optionally at a target.",
            "parameters": {
                "type": "object",
                "properties": {
                    "spell_id": {"type": "integer"},
                    "target_guid": {"type": "integer"},
                },
                "required": ["spell_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "auto_attack",
            "description": "Start auto-attacking a target.",
            "parameters": {
                "type": "object",
                "properties": {"guid": {"type": "integer"}},
                "required": ["guid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_attack",
            "description": "Stop attacking the current target.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_item",
            "description": "Use or consume an item from the inventory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bag": {"type": "integer"},
                    "slot": {"type": "integer"},
                    "target_guid": {"type": "integer"},
                },
                "required": ["bag", "slot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "equip_item",
            "description": "Equip an item from the inventory.",
            "parameters": {
                "type": "object",
                "properties": {"bag": {"type": "integer"}, "slot": {"type": "integer"}},
                "required": ["bag", "slot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sell_item",
            "description": "Sell an item to the currently open vendor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_guid": {"type": "integer"},
                    "bag": {"type": "integer"},
                    "slot": {"type": "integer"},
                    "count": {"type": "integer"},
                },
                "required": ["vendor_guid", "bag", "slot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "interact",
            "description": "Open an NPC's dialog, loot, or use a gameobject.",
            "parameters": {
                "type": "object",
                "properties": {"guid": {"type": "integer"}},
                "required": ["guid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gossip_select",
            "description": "Choose a gossip menu option in the currently open NPC window.",
            "parameters": {
                "type": "object",
                "properties": {"option_index": {"type": "integer"}},
                "required": ["option_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "accept_quest",
            "description": "Accept an available quest.",
            "parameters": {
                "type": "object",
                "properties": {"quest_id": {"type": "integer"}},
                "required": ["quest_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "turn_in_quest",
            "description": "Complete and turn in a finished quest.",
            "parameters": {
                "type": "object",
                "properties": {
                    "quest_id": {"type": "integer"},
                    "reward_choice": {"type": "integer"},
                },
                "required": ["quest_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abandon_quest",
            "description": "Drop a quest from the quest log.",
            "parameters": {
                "type": "object",
                "properties": {"quest_id": {"type": "integer"}},
                "required": ["quest_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "loot",
            "description": "Loot a corpse or object by GUID.",
            "parameters": {
                "type": "object",
                "properties": {"guid": {"type": "integer"}},
                "required": ["guid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "loot_item",
            "description": "Take a specific item from an open loot window.",
            "parameters": {
                "type": "object",
                "properties": {"guid": {"type": "integer"}, "slot": {"type": "integer"}},
                "required": ["guid", "slot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "say",
            "description": "Speak in local /say chat.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "whisper",
            "description": "Send a private whisper to a player.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_name": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["target_name", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "emote",
            "description": "Play an emote.",
            "parameters": {
                "type": "object",
                "properties": {"emote_id": {"type": "integer"}},
                "required": ["emote_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "accept_group",
            "description": "Accept a pending party invite.",
            "parameters": {
                "type": "object",
                "properties": {"invite_id": {"type": "integer"}},
                "required": ["invite_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "leave_group",
            "description": "Leave the current party.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_mail",
            "description": "Send in-game mail, optionally with items or gold.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["recipient", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_flight_path",
            "description": "Take a flight path (must be at a flight master).",
            "parameters": {
                "type": "object",
                "properties": {"node_id": {"type": "integer"}},
                "required": ["node_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "learn_talent",
            "description": "Spend a talent point.",
            "parameters": {
                "type": "object",
                "properties": {"talent_id": {"type": "integer"}},
                "required": ["talent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_goal",
            "description": "Meta-tool: update the agent's current high-level goal string.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Meta-tool: do nothing this cycle (staying idle/silent is a valid choice).",
            "parameters": {
                "type": "object",
                "properties": {"seconds": {"type": "number", "maximum": 30}},
            },
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
