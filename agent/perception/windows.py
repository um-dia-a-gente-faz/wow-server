"""WorldState's quest-giver, NPC-window, mailbox and chat-channel state (UM-40, UM-41, UM-60, UM-93)."""

class WindowsMixin:
    # ── Quests (UM-41) ─────────────────────────────────────────────────────

    def apply_questgiver_status(self, data: dict):
        """SMSG_QUESTGIVER_STATUS (agent.quests.parse_questgiver_status):
        backfill the matching NPC's quest_giver_status, exposed to the LLM
        via snapshot()'s nearby_units/nearby_players entries."""
        with self._lock:
            obj = self.objects.get(data["guid"])
            if obj is not None:
                obj.quest_giver_status = data["status"]
                obj.quest_giver_status_name = data["status_name"]
            self._quest_status_in_flight.discard(data["guid"])

    def apply_questgiver_quest_list(self, data: dict):
        """SMSG_QUESTGIVER_QUEST_LIST: opens the questgiver's quest-list
        window (available/offered quests at this NPC)."""
        with self._lock:
            self.ui_state = {"kind": "quest_list", **data}

    def apply_questgiver_quest_details(self, data: dict):
        """SMSG_QUESTGIVER_QUEST_DETAILS: opens a single quest's detail
        view (full text, before accepting)."""
        with self._lock:
            self.ui_state = {"kind": "quest_details", **data}

    def apply_questgiver_request_items(self, data: dict):
        """SMSG_QUESTGIVER_REQUEST_ITEMS: the "turn this quest in" window —
        shown in response to CMSG_QUESTGIVER_COMPLETE_QUEST when the
        server wants confirmation of the required items/money before
        offering the reward."""
        with self._lock:
            self.ui_state = {"kind": "quest_request_items", **data}

    def apply_questgiver_offer_reward(self, data: dict):
        """SMSG_QUESTGIVER_OFFER_REWARD: the reward-choice window —
        CMSG_QUESTGIVER_CHOOSE_REWARD (turn_in_quest) reads its
        reward_choice_items from here."""
        with self._lock:
            self.ui_state = {"kind": "quest_offer_reward", **data}

    def apply_quest_query_response(self, data: dict):
        """SMSG_QUEST_QUERY_RESPONSE (agent.quests.parse_quest_query_response):
        cache this quest's static text/reward data — consumed by
        build_quest_log() below to enrich quest_log entries with title/
        objective text and used by any window that only carries a bare
        quest id."""
        with self._lock:
            self.quest_texts.on_response(data)


    def apply_gossip_message(self, data: dict):
        """SMSG_GOSSIP_MESSAGE (agent.npc.parse_gossip_message): opens (or
        replaces) the gossip window. If the npc text for this menu's
        text_id is already cached, attach its first option's text as
        `body_text`; otherwise queue a CMSG_NPC_TEXT_QUERY (drained by
        session.py like the name cache)."""
        with self._lock:
            window = {"kind": "gossip", **data}
            cached = self.npc_texts.texts.get(data["text_id"])
            if cached:
                window["body_text"] = _first_npc_text(cached)
            else:
                self.npc_texts.want(data["text_id"], data["npc_guid"])
            self.ui_state = window

    def apply_gossip_complete(self):
        """SMSG_GOSSIP_COMPLETE: the server closed the gossip window (e.g.
        after gossip_select on a plain "go away" option)."""
        with self._lock:
            self.ui_state = None

    def apply_list_inventory(self, data: dict):
        """SMSG_LIST_INVENTORY (agent.npc.parse_list_inventory): opens the
        vendor window."""
        with self._lock:
            self.ui_state = {"kind": "vendor", **data}

    def apply_trainer_list(self, data: dict):
        """SMSG_TRAINER_LIST (agent.npc.parse_trainer_list): opens the
        trainer window."""
        with self._lock:
            self.ui_state = {"kind": "trainer", **data}

    def apply_npc_text_update(self, data: dict):
        """SMSG_NPC_TEXT_UPDATE (agent.npc.parse_npc_text_update): backfill
        the currently-open gossip window's body_text if it's still waiting
        on this text_id."""
        with self._lock:
            self.npc_texts.on_response(data)
            if data["found"] and self.ui_state is not None \
                    and self.ui_state.get("kind") == "gossip" \
                    and self.ui_state.get("text_id") == data["text_id"]:
                self.ui_state["body_text"] = _first_npc_text(data)

    def close_window(self):
        """Local-only close (the client doesn't need server confirmation to
        stop showing a window) — used by actions.CloseWindowAction."""
        with self._lock:
            self.ui_state = None

    def apply_item_query_response(self, data: dict):
        """SMSG_ITEM_QUERY_SINGLE_RESPONSE (UM-42): backfill every
        currently-known item/container with this entry, same policy as
        apply_creature_query_response."""
        with self._lock:
            self.items.on_item_query_response(data)
            if data["found"]:
                for obj in self.objects.values():
                    if obj.object_type in ("item", "container") and obj.entry == data["entry"]:
                        obj.name = data["name"]

    # ── Mailbox (UM-60) ────────────────────────────────────────────────────

    def _resolve_attachment_name(self, item: dict) -> dict:
        """entry -> name best-effort via the item-template cache (same
        source build_equipment_and_inventory uses), queuing a query for an
        unresolved entry — called with self._lock already held."""
        cached = self.items.items.get(item["entry"])
        if cached is not None:
            name = cached["name"]
        else:
            name = None
            self.items.want_item(item["entry"])
        return {**item, "name": name}

    def open_mailbox_request(self, mailbox_guid: int):
        """Called by actions.OpenMailboxAction right before sending
        CMSG_GET_MAIL_LIST — optimistically records which guid this is for
        (SMSG_MAIL_LIST_RESULT itself doesn't carry the mailbox guid back),
        the same "set local state, let the server reply fill it in" shape
        as agent.perception's trade-request handling."""
        with self._lock:
            self.mailbox = {"mailbox_guid": mailbox_guid, "total_records": None, "mails": None}

    def apply_mail_list_result(self, data: dict):
        """SMSG_MAIL_LIST_RESULT (agent.mail.parse_mail_list_result): fills
        in the mailbox window opened by open_mailbox_request — agent/actions/mail.py's
        take_mail/delete_mail read `mails`/`mailbox_guid` from here. Ignored
        if nothing is pending (a stale/unexpected reply). Clears
        has_new_mail the same way a real client's mail icon clears once you
        open the mailbox."""
        with self._lock:
            if self.mailbox is None:
                return
            mails = [{**m, "attachments": [self._resolve_attachment_name(a) for a in m["attachments"]]}
                     for m in data["mails"]]
            self.mailbox["total_records"] = data["total_records"]
            self.mailbox["mails"] = mails
            self.has_new_mail = False

    def apply_received_mail(self, data: dict):
        """SMSG_RECEIVED_MAIL (agent.mail.parse_received_mail): new mail
        arrived — surfaced to the LLM via snapshot()'s `has_new_mail` flag."""
        with self._lock:
            self.has_new_mail = True

    def apply_channel_notify(self, data: dict):
        """SMSG_CHANNEL_NOTIFY (agent.channels.parse_channel_notify): track
        you_joined/you_left. A zone change re-joins General under the new
        zone's name *without* a you_left for the old one
        (Player::UpdateLocalChannels, sendRemove = false), so a you_joined
        for a system channel replaces any entry with the same channel_id."""
        notice = data.get("notice_name")
        with self._lock:
            if notice == "you_joined":
                cid = data.get("channel_id", 0)
                if cid:
                    for full in [n for n, c in self.channels.items() if c["channel_id"] == cid]:
                        del self.channels[full]
                self.channels[data["channel"]] = {"channel_id": cid, "flags": data.get("flags", 0)}
            elif notice == "you_left":
                self.channels.pop(data["channel"], None)

    def get_channels(self) -> dict[str, dict]:
        with self._lock:
            return dict(self.channels)

    def get_mailbox(self) -> dict | None:
        with self._lock:
            return self.mailbox


def _first_npc_text(data: dict) -> str:
    """Pick the first non-empty option's text0 out of a parsed
    SMSG_NPC_TEXT_UPDATE (agent.npc.parse_npc_text_update) — real servers
    fill option 0 for a plain gossip greeting; the remaining 7 slots
    (MAX_GOSSIP_TEXT_OPTIONS) are usually empty placeholders for randomized
    flavor text, which this v1 doesn't attempt to pick between."""
    for option in data.get("options", []):
        if option["text0"]:
            return option["text0"]
    return ""
