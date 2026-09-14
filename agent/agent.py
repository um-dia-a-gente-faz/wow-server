#!/usr/bin/env python3
"""Agent loop — login, perceive, act. MVP: send chat messages, track objects."""

import sys
import time

from .auth import auth_logon
from .session import WoWSession


def run_agent(username: str, password: str, char_guid: int = 0,
              host: str = "192.168.1.64", auth_port: int = 3724,
              duration: float = 15.0):
    account, key, realms = auth_logon(host, auth_port, username, password)
    if not account:
        print("AUTH FAILED"); return
    r = list(realms.values())[0]
    wh, ps = r['address'].rsplit(':', 1)

    sess = WoWSession(wh, int(ps), account, key, r['id'])
    sess.connect()
    chars = sess.enum_characters()
    for c in chars:
        print(f"  [{c['guid']}] {c['name']} L{c['level']}")

    choice = next((c for c in chars if c['guid'] == char_guid), chars[0]) if chars else None
    if not choice:
        print("No characters"); return

    sess.login_character(choice['guid'])
    print(f"Online at {sess.player_position}")

    from . import actions
    time.sleep(1)
    actions.send_chat_message(sess, "Agent reporting for duty o/")

    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        pass

    sess.logout()
    print("Done.")