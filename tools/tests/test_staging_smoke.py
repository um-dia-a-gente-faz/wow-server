"""#795: tools/agent_dev/staging_smoke.py against a stub login. The smoke's steps and its refusals are checked
here; the live run against staging is a human step. Loaded by path, so this file needs no sys.path
edit, and the stub stands in for agent.login (nothing here touches a socket)."""
import importlib.util
import io
import pathlib
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("staging_smoke", ROOT / "tools" / "agent_dev" / "staging_smoke.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

SENTINEL = "sentinel-795"
STAGING_ENV = {"WOW_HOST": "192.0.2.10", "WOW_AUTH_PORT": "3725", "WOW_ACCOUNT": "STAGING01",
               "WOW_PASSWORD": SENTINEL}


class FakeSession:
    def __init__(self, player_guid=42, drops=False):
        self.player_guid = player_guid
        self.unexpected_disconnect = drops
        self.logouts = 0

    def logout(self):
        self.logouts += 1


class StubLogin:
    """Hands out one FakeSession per call; `fail_on` is the 1-based call that raises."""

    def __init__(self, fail_on=0, **session_kw):
        self.calls = 0
        self.fail_on = fail_on
        self.session_kw = session_kw
        self.sessions = []

    def __call__(self, cfg, log):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("auth server rejected the logon proof: unknown account or wrong password "
                               "(AuthResult 0x04)")
        sess = FakeSession(**self.session_kw)
        self.sessions.append(sess)
        return sess


def run_smoke(login, env=STAGING_ENV):
    """Runs main() with the given stub login. Returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    # the password rides on the cfg the smoke hands to login, as in a real run: a print of cfg.password shows here
    cfg = SimpleNamespace(wow_host=env.get("WOW_HOST"), wow_auth_port=env.get("WOW_AUTH_PORT"),
                          account=env.get("WOW_ACCOUNT"), password=SENTINEL)
    with redirect_stdout(out), mock.patch("sys.stderr", err):
        code = smoke.main(env=env, load=lambda: cfg, login=login)
    return code, out.getvalue(), err.getvalue()


class SmokeRunTest(unittest.TestCase):
    def test_all_six_steps_pass_and_both_sessions_log_out(self):
        login = StubLogin()
        code, out, _ = run_smoke(login)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), [
            "PASS login", "PASS enter world", "PASS logout",
            "PASS reconnect", "PASS enter world (2)", "PASS logout (2)"])
        self.assertEqual(login.calls, 2)              # a real reconnect: a second login
        self.assertEqual([s.logouts for s in login.sessions], [1, 1])

    def test_a_failed_login_stops_at_once(self):
        login = StubLogin(fail_on=1)
        code, out, _ = run_smoke(login)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [
            "FAIL login: RuntimeError: auth server rejected the logon proof: unknown account or wrong "
            "password (AuthResult 0x04)"])
        self.assertEqual(login.calls, 1)

    def test_a_failed_reconnect_fails_the_smoke_and_leaves_nothing_open(self):
        login = StubLogin(fail_on=2)
        code, out, _ = run_smoke(login)
        self.assertEqual(code, 1)
        self.assertIn("PASS logout\n", out)
        self.assertIn("FAIL reconnect", out)
        self.assertNotIn("logout (2)", out)
        self.assertEqual(login.sessions[0].logouts, 1)   # the first logout ran once, not again

    def test_no_character_guid_fails_enter_world_and_the_session_is_logged_out(self):
        login = StubLogin(player_guid=0)
        code, out, _ = run_smoke(login)
        self.assertEqual(code, 1)
        self.assertIn("FAIL enter world: RuntimeError: no character guid after login", out)
        self.assertEqual(login.sessions[0].logouts, 1)   # cleanup: the agent is not left in the world

    def test_a_dropped_session_fails_the_logout(self):
        login = StubLogin(drops=True)
        code, out, _ = run_smoke(login)
        self.assertEqual(code, 1)
        self.assertIn("FAIL logout: RuntimeError: the server dropped the session", out)

    def test_the_password_is_never_printed(self):
        login = StubLogin(fail_on=1)
        code, out, err = run_smoke(login)
        self.assertNotIn(SENTINEL, out + err)
        ok_code, ok_out, ok_err = run_smoke(StubLogin())
        self.assertNotIn(SENTINEL, ok_out + ok_err)


class ConfigTest(unittest.TestCase):
    def test_staging_settings_are_accepted(self):
        self.assertEqual(smoke.config_problems(STAGING_ENV), [])

    def test_missing_settings_are_named_and_exit_2(self):
        for name in smoke.REQUIRED:
            with self.subTest(name=name):
                env = {k: v for k, v in STAGING_ENV.items() if k != name}
                self.assertEqual(smoke.config_problems(env), [f"{name} is required"])
        login = StubLogin()
        code, _, err = run_smoke(login, env={"WOW_HOST": "192.0.2.10"})
        self.assertEqual(code, 2)
        self.assertIn("WOW_AUTH_PORT is required", err)
        self.assertEqual(login.calls, 0)

    def test_the_live_realm_is_refused_by_host_or_port(self):
        live_host = {**STAGING_ENV, "WOW_HOST": "192.168.1.64"}
        live_port = {**STAGING_ENV, "WOW_AUTH_PORT": "3724"}
        for env in (live_host, live_port):
            with self.subTest(env=env):
                self.assertTrue(any("refusing the live realm" in p for p in smoke.config_problems(env)))
        login = StubLogin()
        code, _, _ = run_smoke(login, env=live_host)
        self.assertEqual(code, 2)
        self.assertEqual(login.calls, 0)

    def test_a_non_numeric_port_is_refused(self):
        problems = smoke.config_problems({**STAGING_ENV, "WOW_AUTH_PORT": "seven"})
        self.assertEqual(len(problems), 1)
        self.assertIn("port number", problems[0])


if __name__ == "__main__":
    unittest.main()
