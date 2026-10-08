"""The `equipment` and `inventory` sections of the snapshot (UM-42)."""

from .. import update_fields as uf


def build_equipment_and_inventory(me, objects, items) -> tuple[dict, list]:
    """UM-42: equipment (slot -> item dict, slots 0-18) and inventory
    (list of item dicts, slots 19-38: 4 equipped-bag-container slots +
    16 backpack slots) built from the self player's own INV_SLOT_HEAD/
    PACK_SLOT_1 guid fields (agent.update_fields.
    decode_equipment_and_inventory_guids) cross-referenced against the
    item objects those guids point to (agent.update_fields.
    decode_item_fields) and the item-template cache `items` for
    names. A slot whose item object hasn't arrived yet (or whose
    template name hasn't resolved) is still included with whatever is
    known (bare guid, or guid+entry without a name).
    Caller holds the WorldState lock; `objects` is its guid -> ObjectInfo dict."""
    if me is None:
        return {}, []
    slot_guids = uf.decode_equipment_and_inventory_guids(me.raw_fields)
    equipment: dict[int, dict] = {}
    inventory: list = []
    for slot, guid in slot_guids.items():
        item_obj = objects.get(guid)
        d = {"guid": guid}
        if item_obj is not None:
            d["entry"] = item_obj.entry
            d["name"] = item_obj.name or None
            template = items.items.get(item_obj.entry) if item_obj.entry is not None else None
            if template:
                # Snapshot consumers need only comparison/offer fields.
                # The cached query response is larger and is serialized
                # into every Jev prompt; keep this projection bounded.
                d["template"] = {key: template[key] for key in (
                    "quality", "inventory_type", "class_", "subclass",
                    "allowable_class", "item_level", "stats", "spells",
                ) if key in template}
            count = item_obj.raw_fields and uf.decode_item_fields(item_obj.raw_fields).get("count")
            if count is not None:
                d["count"] = count
        if slot < uf.EQUIPMENT_SLOT_COUNT:
            equipment[slot] = d
        else:
            d["slot"] = slot
            inventory.append(d)
    return equipment, inventory
