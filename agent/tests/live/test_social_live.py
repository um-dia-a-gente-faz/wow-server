"""Opt-in live test against the real server — absorbs the old root-level
test_social.py (login -> whisper -> party invite), fixed to use the real
chat wire format (agent/actions/chat.py) instead of that script's placeholder
whisper target and CHAT_MSG_PARTY(2)-as-whisper bug.

Skipped unless WOW_LIVE_TESTS=1. Needs WOW_ACCOUNT/WOW_PASSWORD/WOW_CHARACTER
(see .env.example) and a second character online to receive the whisper/
invite (default "Rubens", override with WOW_LIVE_TEST_TARGET).

Run: WOW_LIVE_TESTS=1 python3 -m unittest agent.tests.live.test_social_live -v
"""

import os
import time
import unittest

from agent import actions
from agent.auth import auth_logon
from agent.config import load_config
from agent.session import WoWSession

LIVE = os.environ.get("WOW_LIVE_TESTS", "").strip() == "1"
TARGET_NAME = os.environ.get("WOW_LIVE_TEST_TARGET", "Rubens")


@unittest.skipUnless(LIVE, "set WOW_LIVE_TESTS=1 to run against the real server")
class SocialLiveTest(unittest.TestCase):
    def setUp(self):
        cfg = load_config()
        problems = cfg.validate()
        if problems:
            self.skipTest(f"config incomplete: {problems}")
        account, session_key, realms = auth_logon(cfg.wow_host, cfg.wow_auth_port,
                                                    cfg.account, cfg.password)
        if not account:
            self.fail("auth failed — check WOW_ACCOUNT/WOW_PASSWORD")
        r = list(realms.values())[0]
        host, port_s = r['address'].rsplit(':', 1)
        self.sess = WoWSession(host, int(port_s), account, session_key, r['id'])
        self.sess.connect()
        chars = self.sess.enum_characters()
        choice = next((c for c in chars if c['name'].lower() == cfg.character.lower()), chars[0])
        self.sess.race = choice['race']
        self.sess.login_character(choice['guid'])

    def tearDown(self):
        self.sess.logout()

    def test_say_yell_whisper_and_party_invite(self):
        # Verify these against tools/chat-feed's SSE stream (an independent
        # oracle for the chat type): curl -N http://<host>:9500/api/chat/stream
        actions.say(self.sess, "Olá, tudo bem? (test_social_live say)")
        time.sleep(1)
        actions.yell(self.sess, "test_social_live yell")
        time.sleep(1)
        actions.whisper(self.sess, TARGET_NAME, f"Olá {TARGET_NAME}! test_social_live whisper")
        time.sleep(1)
        actions.invite_to_group(self.sess, TARGET_NAME)
        time.sleep(2)
        # No assertion beyond "didn't raise" — confirming these arrived with
        # the right kind/target needs a human watching the chat-feed stream
        # or the target's client (see the card's acceptance criteria).


if __name__ == '__main__':
    unittest.main()
