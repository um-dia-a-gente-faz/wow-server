"""Inventory rows -> the API's item JSON, with the DBC-backed tooltips."""
import item_tooltip
import state
from repo import characters as characters_repo


def item_set_context(cur, rows):
    """(entries the character has equipped, {item set piece entry: name}) for the item
    set block of the tooltips. Equipped = bag 0, slots 0..18 (Player.h EQUIPMENT_SLOT_*);
    piece names come from world.item_template (ItemSet.dbc only has the entries)."""
    equipped = {r[3] for r in rows if r[0] == 0 and 0 <= r[1] < 19}
    col = 10 + item_tooltip.COLUMNS.index("itemset")
    entries = item_tooltip.set_piece_entries({r[col] for r in rows if r[col]}, state.names())
    if not entries:
        return equipped, {}
    return equipped, dict(characters_repo.item_names(cur, entries))


def inventory_item(row, n=None, equipped=(), set_names=None):
    """One inventory row: (bag, slot, item guid, entry, name, count, displayid, Quality,
    item_instance.flags, item_instance.durability, *item_tooltip.COLUMNS,
    item_instance.randomPropertyId, item_instance.enchantments). `n` (GameNames) adds
    the DBC-backed tooltip lines; `equipped`/`set_names` feed the item set block."""
    (bag, slot, item_guid, item_entry, item_name, count, display_id, quality,
     inst_flags, durability) = row[:10]
    n_cols = len(item_tooltip.COLUMNS)
    template = dict(zip(item_tooltip.COLUMNS, row[10:10 + n_cols]))
    random_property, enchantments = (tuple(row[10 + n_cols:]) + (0, ""))[:2]
    return {
        "bag": bag, "slot": slot, "item_guid": item_guid, "item_entry": item_entry,
        "item_name": item_name, "count": count,
        # Quality is item_template.Quality (0 poor .. 7 heirloom), None if unknown.
        "quality": quality, "icon": state.icon_url(display_id),
        # A bag's size (item_template.ContainerSlots), for drawing its grid; 0 otherwise.
        "container_slots": template.get("ContainerSlots") or 0,
        "tooltip": item_tooltip.tooltip(
            item_name, template, count, inst_flags, durability, names=n,
            random_property=random_property or 0, enchantments=enchantments or "",
            equipped=equipped, item_names=set_names),
    }
