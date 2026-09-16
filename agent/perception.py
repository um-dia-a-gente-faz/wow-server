#!/usr/bin/env python3
"""World state: tracks nearby objects from UpdateObject packets."""

import threading


class PerceptionParseError(Exception):
    """A server packet could not be parsed into world state.

    Raised for truncated or malformed update-object data. The session drops
    the offending packet and keeps the connection alive.
    """


class WorldState:
    """Thread-safe container for perceived game objects.

    Every dict is keyed by the 64-bit GUID as an int.
    """

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
        """Remember which GUID is our own character. Does not create an object;
        our player shows up through its update-object block like anything else."""
        with self._lock:
            self.my_guid = guid


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
