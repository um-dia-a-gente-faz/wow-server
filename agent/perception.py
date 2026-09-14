#!/usr/bin/env python3
"""World state: tracks nearby objects from UpdateObject packets."""

import threading


class WorldState:
    """Thread-safe container for perceived game objects."""

    def __init__(self):
        self._lock = threading.Lock()
        self.objects = {}       # guid -> ObjectInfo
        self.creatures = {}     # guid -> CreatureInfo
        self.players = {}       # guid -> PlayerInfo
        self.my_guid = 0

    def record_guid(self, guid: int, update_type: int):
        """Track that a GUID exists."""
        with self._lock:
            if guid not in self.objects:
                self.objects[guid] = ObjectInfo(guid=guid, update_type=update_type)

    def get_objects(self) -> dict:
        with self._lock:
            return dict(self.objects)

    def set_my_guid(self, guid: int):
        with self._lock:
            self.my_guid = guid
            guid_str = f"0x{guid:016x}"
            self.objects[guid_str] = ObjectInfo(guid=guid, update_type=0)


class ObjectInfo:
    __slots__ = ('guid', 'update_type', 'name', 'position', 'level', 'health')

    def __init__(self, guid=0, update_type=0):
        self.guid = guid
        self.update_type = update_type
        self.name = ""
        self.position = None  # (x, y, z, map_id)
        self.level = 0
        self.health = 0

    def __repr__(self):
        return f"<Object #{self.guid:x} {self.name or '?'}>"