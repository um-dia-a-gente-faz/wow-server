"""WorldState's name/template resolution: fills object names from the caches and queues queries (UM-35)."""

from .objects import ObjectInfo


class ResolutionMixin:
    def _maybe_resolve_name(self, obj: ObjectInfo):
        """UM-35: fill obj.name (etc.) from the cache if already known,
        otherwise enqueue a query. Called with self._lock already held.
        Skips our own object — we already know our name; nothing queries it."""
        if obj.guid == self.my_guid:
            return
        if obj.object_type == "player":
            if obj.guid in self.names.players:
                name = self.names.players[obj.guid]
                if name is not None:
                    obj.name = name
            else:
                self.names.want_player(obj.guid)
        elif obj.object_type == "unit" and obj.entry:
            if obj.entry in self.names.creatures:
                data = self.names.creatures[obj.entry]
                if data is not None:
                    self._apply_creature_name(obj, data)
            else:
                self.names.want_creature(obj.entry, obj.guid)
        elif obj.object_type == "gameobject" and obj.entry:
            if obj.entry in self.names.gameobjects:
                data = self.names.gameobjects[obj.entry]
                if data is not None:
                    obj.name = data["name"]
                    # UM-60: is_mailbox() needs `type` too — found live testing:
                    # a *disk-cached* gameobject template (agent.names.NameCache
                    # persists creature/gameobject templates across runs) hit
                    # this branch and backfilled only .name, leaving
                    # .gameobject_type permanently None even though the type
                    # was sitting right there in the same cached dict.
                    obj.gameobject_type = data["type"]
            else:
                self.names.want_gameobject(obj.entry, obj.guid)
        elif obj.object_type in ("item", "container") and obj.entry:
            if obj.entry in self.items.items:
                data = self.items.items[obj.entry]
                if data is not None:
                    obj.name = data["name"]
            else:
                self.items.want_item(obj.entry)

        # UM-41: any unit/player with the questgiver npc flag and no status
        # yet gets a CMSG_QUESTGIVER_STATUS_QUERY queued, same trigger point
        # as the name/npc-text queries above (called every time a CREATE or
        # a fresh VALUES update touches this object).
        if obj.is_quest_giver() and obj.quest_giver_status is None \
                and obj.guid not in self._quest_status_in_flight:
            self._quest_status_in_flight.add(obj.guid)
            self._quest_status_pending.append(obj.guid)

    def drain_quest_giver_status_queries(self, max_items: int | None = None) -> list:
        """Pop queued questgiver guids to CMSG_QUESTGIVER_STATUS_QUERY —
        drained by session.py once per recv-loop tick, like
        names/npc_texts/quest_texts. No send-rate budget: this queue only
        grows from newly-perceived questgivers, which in practice arrive at
        a trickle, not a flood."""
        with self._lock:
            if max_items is None:
                out, self._quest_status_pending = self._quest_status_pending, []
                return out
            out = self._quest_status_pending[:max_items]
            self._quest_status_pending = self._quest_status_pending[max_items:]
            return out

    def resolve_player_name(self, guid: int) -> str | None:
        """UM-47: cached name for a player GUID, queuing a CMSG_NAME_QUERY
        the first time it's asked for.

        Returns None while the name is unknown — either because the query
        hasn't been answered yet, or because the server said "no such
        player" (NameCache stores that as None). Used by the chat relay,
        whose senders are GUIDs the agent may never have had in range:
        `SMSG_MESSAGECHAT` carries no sender name outside the GM opcode.
        """
        with self._lock:
            if guid in self.names.players:
                return self.names.players[guid]
            self.names.want_player(guid)
            return None

    @staticmethod
    def _apply_creature_name(obj: ObjectInfo, data: dict):
        obj.name = data["name"]
        obj.subname = data.get("subname", "")
        obj.rank = data.get("rank", "") if data.get("rank") != "normal" else ""
        obj.creature_type = data.get("creature_type_name")

    def apply_name_query_response(self, data: dict):
        """SMSG_NAME_QUERY_RESPONSE (agent.names.parse_name_query_response):
        backfill .name on the matching player object, if it's still around."""
        with self._lock:
            self.names.on_name_query_response(data)
            if data["found"]:
                obj = self.objects.get(data["guid"])
                if obj is not None:
                    obj.name = data["name"]

    def apply_creature_query_response(self, data: dict):
        """SMSG_CREATURE_QUERY_RESPONSE: backfill every currently-known unit
        with this entry (there can be several, e.g. six identical "Shaker"
        NPCs — see agent/tests/fixtures/update_object/README.md)."""
        with self._lock:
            self.names.on_creature_query_response(data)
            if data["found"]:
                for obj in self.objects.values():
                    if obj.object_type == "unit" and obj.entry == data["entry"]:
                        self._apply_creature_name(obj, data)

    def apply_gameobject_query_response(self, data: dict):
        """SMSG_GAMEOBJECT_QUERY_RESPONSE: backfill every currently-known
        gameobject with this entry — name and, since UM-60, `type` (e.g.
        GAMEOBJECT_TYPE_MAILBOX), the only way ObjectInfo.is_mailbox() can
        tell a mailbox gameobject apart from any other."""
        with self._lock:
            self.names.on_gameobject_query_response(data)
            if data["found"]:
                for obj in self.objects.values():
                    if obj.object_type == "gameobject" and obj.entry == data["entry"]:
                        obj.name = data["name"]
                        obj.gameobject_type = data["type"]
