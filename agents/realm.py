"""The live side of agent accounts (#391): log an account in to the realm, and create an
account through the worldserver console.

Stdlib only at import time. Realm.open imports the `agent` package of the same checkout
inside the method, so importing this module opens no socket; the caller must have the
checkout root on sys.path. Used by scripts/create_agent_roster.py and tools/agent-runner.
"""
import json
import os
import time
import urllib.request


class Console:
    """Fire-and-forget `account create` over the worldserver's socket.io console: the same
    engine.io polling requests as scripts/wow_console.py, written out here so that no
    library imports a script.

    The command carries a password, so it is never printed and the server's reply is never
    read back; callers confirm by logging in afterwards."""

    def __init__(self, host: str):
        self.host = host

    def create_account(self, account: str, password: str) -> None:
        base = f"http://{self.host}:{os.environ.get('WOW_PORT', '3000')}/socket.io/"
        with urllib.request.urlopen(f"{base}?EIO=4&transport=polling&t={int(time.time() * 1000)}",
                                    timeout=10) as r:
            hs = r.read().decode()
        sid = json.loads(hs[hs.find("{"):])["sid"]
        self._post(base, sid, "40")
        time.sleep(0.5)
        with urllib.request.urlopen(f"{base}?EIO=4&transport=polling&sid={sid}&t={int(time.time() * 1000)}",
                                    timeout=10) as r:
            r.read()
        self._post(base, sid, "42" + json.dumps(["worldserver_input", f"account create {account} {password}"]))
        time.sleep(2.0)

    @staticmethod
    def _post(base: str, sid: str, payload: str) -> None:
        req = urllib.request.Request(f"{base}?EIO=4&transport=polling&sid={sid}", data=payload.encode(),
                                     method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()


class Realm:
    """One authenticated world session for an account (no character selected)."""

    def __init__(self, host: str, auth_port: int):
        self.host, self.auth_port = host, auth_port

    def open(self, account: str, password: str):
        """Returns a connected WoWSession, or None if the account/password is rejected."""
        from agent.auth import AuthRejected, auth_logon
        from agent.session import WoWSession
        try:
            acc, key, realms = auth_logon(self.host, self.auth_port, account, password)
        except AuthRejected:
            return None
        realm = list(realms.values())[0]
        world_host, port = realm["address"].rsplit(":", 1)
        sess = WoWSession(world_host, int(port), acc, key, realm["id"])
        sess.connect()
        return sess
