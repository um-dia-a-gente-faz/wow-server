"""One spec per registered action: the only place its name, parameters, required parameters and
per-snapshot candidate cap are written (gh-419). Action classes take `params`/`required` from here,
the LLM tool schema and `catalog()` are generated from it, validation in think.py reads it, and
agent.action_names re-exports the name constants below. A test checks every spec against its
registered class. Pure values; imports nothing from agent/ (specs sit below actions and candidates).

`all_specs` is in registration order (= the order of catalog(), which the LLM sees): keep it."""

from dataclasses import dataclass, field

SET_TARGET = "set_target"
FACE = "face"
MOVE_TO = "move_to"
MOVE_TOWARDS = "move_towards"
STOP_MOVEMENT = "stop_movement"
AUTO_ATTACK = "auto_attack"
STOP_ATTACK = "stop_attack"
IDLE = "idle"
CAST_SPELL = "cast_spell"
INTERACT = "interact"
GOSSIP_SELECT = "gossip_select"
BUY_ITEM = "buy_item"
SELL_ITEM = "sell_item"
TRAIN_SPELL = "train_spell"
INVITE_TO_GROUP = "invite_to_group"
ACCEPT_GROUP = "accept_group"
DECLINE_GROUP = "decline_group"
LEAVE_GROUP = "leave_group"
PROMOTE_LEADER = "promote_leader"
CLOSE_WINDOW = "close_window"
LOOT = "loot"
USE_ITEM = "use_item"
DESTROY_ITEM = "destroy_item"
COMPARE_ITEMS = "compare_items"
EQUIP_ITEM = "equip_item"
ACCEPT_QUEST = "accept_quest"
COMPLETE_QUEST = "complete_quest"
TURN_IN_QUEST = "turn_in_quest"
ABANDON_QUEST = "abandon_quest"
OPEN_TRADE = "open_trade"
ACCEPT_TRADE_REQUEST = "accept_trade_request"
OFFER_ITEM = "offer_item"
OFFER_GOLD = "offer_gold"
ACCEPT_TRADE = "accept_trade"
CANCEL_TRADE = "cancel_trade"
OPEN_MAILBOX = "open_mailbox"
SEND_MAIL = "send_mail"
TAKE_MAIL = "take_mail"
DELETE_MAIL = "delete_mail"
RELEASE_SPIRIT = "release_spirit"
RECLAIM_CORPSE = "reclaim_corpse"
FOLLOW = "follow"
ASSIST = "assist"
STOP_FOLLOWING = "stop_following"
REST = "rest"


@dataclass(frozen=True)
class ActionSpec:
    name: str
    params: dict = field(default_factory=dict)   # JSON-schema `properties`, in tool-schema order
    required: tuple = ()                         # subset of params; exact-one-of rules live in check()
    cap: int | None = None                       # most candidates of this action offered per snapshot

    def schema(self, description: str) -> dict:
        """The LLM tool entry. `description` is prose and stays on the Action class."""
        return {
            "name": self.name,
            "description": description,
            "parameters": {"type": "object", "properties": self.params, "required": list(self.required)},
        }


all_specs: tuple[ActionSpec, ...] = (
    ActionSpec(
        SET_TARGET,
        params={
            "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the object to target."},
        },
        required=("guid",),
    ),
    ActionSpec(
        FACE,  # no `required`: exactly one of guid or (x and y), enforced in the action's check()
        params={
            "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the object to face. Mutually exclusive with x/y."},
            "x": {"type": "number", "description": "Target X coordinate. Requires y; mutually exclusive with guid."},
            "y": {"type": "number", "description": "Target Y coordinate. Requires x; mutually exclusive with guid."},
        },
    ),
    ActionSpec(
        MOVE_TO,
        params={
            "x": {"type": "number", "description": "Destination X coordinate."},
            "y": {"type": "number", "description": "Destination Y coordinate."},
            "z": {"type": "number", "description": "Destination Z coordinate. Optional — omit to stay level with the current height (no terrain data in v1)."},
            "stop_distance": {"type": "number", "description": "How close counts as arrived, in yards. Default 1.0."},
        },
        required=("x", "y"),
    ),
    ActionSpec(
        MOVE_TOWARDS,
        params={
            "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the object to move towards."},
            "stop_distance": {"type": "number", "description": "How close counts as arrived, in yards. Default 1.0."},
        },
        required=("guid",),
    ),
    ActionSpec(
        STOP_MOVEMENT,
        params={
        },
    ),
    ActionSpec(
        AUTO_ATTACK,
        params={
            "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the unit to attack."},
        },
        required=("guid",),
    ),
    ActionSpec(
        STOP_ATTACK,
        params={
        },
    ),
    ActionSpec(
        IDLE,
        params={
        },
    ),
    ActionSpec(
        CAST_SPELL,
        params={
            "spell_id": {"type": "integer", "description": "Spell ID to cast — must be in the agent's spellbook."},
            "target_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the unit to cast on. Omit to cast on self."},
        },
        required=("spell_id",),
        cap=3,
    ),
    ActionSpec(
        INTERACT,
        params={
            "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the NPC or gameobject to interact with."},
        },
        required=("guid",),
        cap=3,
    ),
    ActionSpec(
        GOSSIP_SELECT,
        params={
            "option_index": {"type": "integer", "description": "The option's `index` from the open gossip window."},
            "code": {"type": "string", "description": "Text for options that require a text-entry box (rare; the window's option marks `coded: true` when needed)."},
        },
        required=("option_index",),
        cap=4,
    ),
    ActionSpec(
        BUY_ITEM,
        params={
            "vendor_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the open vendor."},
            "slot": {"type": "integer", "description": "Vendor slot from the open window's items list."},
            "entry": {"type": "integer", "description": "Item entry, as an alternative to slot."},
            "count": {"type": "integer", "description": "How many to buy. Default 1."},
        },
        required=("vendor_guid",),
    ),
    ActionSpec(
        SELL_ITEM,
        params={
            "vendor_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the vendor to sell to."},
            "bag": {"type": "integer", "description": "Bag byte of the item (255 = equipped items/backpack)."},
            "slot": {"type": "integer", "description": "Inventory slot of the item."},
            "count": {"type": "integer", "description": "How many to sell from the stack. Default: whole stack."},
        },
        required=("vendor_guid", "bag", "slot"),
        cap=3,
    ),
    ActionSpec(
        TRAIN_SPELL,
        params={
            "trainer_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the open trainer."},
            "spell_id": {"type": "integer", "description": "Spell ID from the open trainer window's spells list."},
        },
        required=("trainer_guid", "spell_id"),
        cap=3,
    ),
    ActionSpec(
        INVITE_TO_GROUP,
        params={
            "name": {"type": "string", "description": "Exact player name to invite."},
        },
        required=("name",),
    ),
    ActionSpec(
        ACCEPT_GROUP,
        params={
        },
    ),
    ActionSpec(
        DECLINE_GROUP,
        params={
        },
    ),
    ActionSpec(
        LEAVE_GROUP,
        params={
        },
    ),
    ActionSpec(
        PROMOTE_LEADER,
        params={
            "name": {"type": "string", "description": "Exact name of the group member to make leader."},
        },
        required=("name",),
    ),
    ActionSpec(
        CLOSE_WINDOW,
        params={
        },
    ),
    ActionSpec(
        LOOT,
        params={
            "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the lootable corpse/creature."},
        },
        required=("guid",),
        cap=3,
    ),
    ActionSpec(
        USE_ITEM,
        params={
            "bag": {"type": "integer", "description": "Bag byte (255 = equipped items/backpack; see CMSG_USE_ITEM's bag field)."},
            "slot": {"type": "integer", "description": "Slot within that bag (0-18 equipment, 19-22 equipped bags, 23-38 backpack)."},
            "target_guid": {"type": "string", "description": "Optional target snapshot handle (a string like \"p1\"), e.g. a bandage used on an ally. Omit to target self."},
        },
        required=("bag", "slot"),
        cap=2,
    ),
    ActionSpec(
        DESTROY_ITEM,
        params={
            "bag": {"type": "integer", "description": "Bag byte (255 = equipped items/backpack)."},
            "slot": {"type": "integer", "description": "Slot within that bag."},
            "count": {"type": "integer", "description": "How many to destroy from the stack."},
            "confirm": {"type": "boolean", "description": "Must be true — a safety guard, not read by the server."},
        },
        required=("bag", "slot", "count", "confirm"),
    ),
    ActionSpec(
        COMPARE_ITEMS,
        params={
            "bag_a": {"type": "integer", "description": "Bag byte of the first item (255 = equipped items/backpack)."},
            "slot_a": {"type": "integer", "description": "Inventory slot of the first item."},
            "bag_b": {"type": "integer", "description": "Bag byte of the second item (255 = equipped items/backpack)."},
            "slot_b": {"type": "integer", "description": "Inventory slot of the second item."},
        },
        required=("bag_a", "slot_a", "bag_b", "slot_b"),
    ),
    ActionSpec(
        EQUIP_ITEM,
        params={
            "bag": {"type": "integer", "description": "Bag byte of the item (255 = equipped items/backpack)."},
            "slot": {"type": "integer", "description": "Inventory slot of the item."},
        },
        required=("bag", "slot"),
        cap=3,
    ),
    ActionSpec(
        ACCEPT_QUEST,
        params={
            "npc_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the questgiver NPC."},
            "quest_id": {"type": "integer", "description": "Quest ID to accept, from the open quest_list/quest_details/gossip window."},
        },
        required=("npc_guid", "quest_id"),
    ),
    ActionSpec(
        COMPLETE_QUEST,
        params={
            "npc_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the questgiver NPC."},
            "quest_id": {"type": "integer", "description": "Quest ID to complete, from quest_log."},
        },
        required=("npc_guid", "quest_id"),
    ),
    ActionSpec(
        TURN_IN_QUEST,
        params={
            "npc_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the questgiver NPC."},
            "quest_id": {"type": "integer", "description": "Quest ID to turn in."},
            "reward_choice": {"type": "integer", "description": "Index of the reward item to pick, from the open window's reward_choice_items. Default 0."},
        },
        required=("npc_guid", "quest_id"),
    ),
    ActionSpec(
        ABANDON_QUEST,
        params={
            "slot": {"type": "integer", "description": "Quest log slot (0-24), from quest_log's `slot` field — not the quest id."},
        },
        required=("slot",),
        cap=2,
    ),
    ActionSpec(
        OPEN_TRADE,
        params={
            "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the player to trade with."},
        },
        required=("guid",),
    ),
    ActionSpec(
        ACCEPT_TRADE_REQUEST,
        params={
        },
    ),
    ActionSpec(
        OFFER_ITEM,
        params={
            "bag": {"type": "integer", "description": "Bag byte of the item (255 = equipped items/backpack)."},
            "slot": {"type": "integer", "description": "Inventory slot of the item."},
            "trade_slot": {"type": "integer", "description": "Which of the 6 trade slots (0-5) to put it in. Default: the first free one."},
        },
        required=("bag", "slot"),
    ),
    ActionSpec(
        OFFER_GOLD,
        params={
            "amount": {"type": "integer", "description": "Copper to offer (100 copper = 1 silver, 10000 = 1 gold). 0 clears a previous offer."},
        },
        required=("amount",),
    ),
    ActionSpec(
        ACCEPT_TRADE,
        params={
            "expected_their_gold": {"type": "integer", "description": "Gold you believe the other side has offered. Default 0."},
            "expected_their_item_entries": {"type": "array", "items": {"type": "integer"}, "description": "Item entries you believe the other side has offered (any order). Omit/empty if you believe they're offering no items."},
        },
    ),
    ActionSpec(
        CANCEL_TRADE,
        params={
        },
    ),
    ActionSpec(
        OPEN_MAILBOX,
        params={
        },
    ),
    ActionSpec(
        SEND_MAIL,
        params={
            "to": {"type": "string", "description": "Recipient character name."},
            "subject": {"type": "string", "description": "Mail subject."},
            "body": {"type": "string", "description": "Mail body text."},
            "gold": {"type": "integer", "description": "Copper to send. Default 0."},
            "bag": {"type": "integer", "description": "Bag byte of an item to attach (255 = equipped items/backpack). Omit to send no item."},
            "slot": {"type": "integer", "description": "Inventory slot of the item to attach — required together with bag."},
        },
        required=("to", "subject", "body"),
    ),
    ActionSpec(
        TAKE_MAIL,
        params={
            "mail_id": {"type": "integer", "description": "Mail id, from the open mailbox window's `mails` list."},
        },
        required=("mail_id",),
    ),
    ActionSpec(
        DELETE_MAIL,
        params={
            "mail_id": {"type": "integer", "description": "Mail id, from the open mailbox window's `mails` list."},
        },
        required=("mail_id",),
    ),
    ActionSpec(
        RELEASE_SPIRIT,
        params={
        },
    ),
    ActionSpec(
        RECLAIM_CORPSE,
        params={
        },
    ),
    ActionSpec(
        FOLLOW,
        params={
            "player_name": {"type": "string", "description": "Name of a nearby player to follow (case-insensitive)."},
            "distance": {"type": "number", "description": "How close to stay, in yards. Default 3.0."},
        },
        required=("player_name",),
        cap=2,
    ),
    ActionSpec(
        ASSIST,
        params={
            "on": {"type": "boolean", "description": "True to assist, false to stop assisting."},
        },
        required=("on",),
    ),
    ActionSpec(
        STOP_FOLLOWING,
        params={
        },
    ),
    ActionSpec(
        REST,
        params={
        },
    ),
)

specs_by_name: dict[str, ActionSpec] = {s.name: s for s in all_specs}


def cap_of(name: str) -> int:
    cap = specs_by_name[name].cap
    assert cap is not None, f"{name} has no cap"
    return cap
