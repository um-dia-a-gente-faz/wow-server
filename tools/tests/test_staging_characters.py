"""#803: tools/agent_dev/staging_characters.py against a stub login. The account rules (a character-less
account gets one, a non-empty account is untouched), the password and the live-realm refusal are checked
here. The stub stands in for agent.login.authenticate_and_login: nothing here touches a socket."""
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from agents.character_rules import HORDE, valid_name
from tools.agent_dev import staging_characters as sc

SENTINEL = "sentinel-803"
STAGING_ENV = {"WOW_HOST": "192.0.2.10", "WOW_AUTH_PORT": "3725", "STAGING_ACCOUNT_PASSWORD": SENTINEL}


class FakeSession:
    def __init__(self, create_error=None):
        self.create_error = create_error
        self.created = []
        self.logouts = 0

    def create_character(self, name, race, class_, *_, **__):
        if self.create_error:
            raise self.create_error
        self.created.append((name, race, class_))
        return 47

    def logout(self):
        self.logouts += 1


class StubLogin:
    """Stands in for authenticate_and_login(cfg, log) -> (session, chars). `chars` maps an account to the
    characters it lists, `fail` to the exception its login raises, `create_fail` to the one its create raises."""

    def __init__(self, chars=None, fail=None, create_fail=None):
        self.chars = chars or {}
        self.fail = fail or {}
        self.create_fail = create_fail or {}
        self.calls = []
        self.sessions = {}

    def __call__(self, cfg, log):
        self.calls.append(cfg.account)
        if cfg.account in self.fail:
            raise self.fail[cfg.account]
        sess = FakeSession(self.create_fail.get(cfg.account))
        self.sessions[cfg.account] = sess
        return sess, [dict(c) for c in self.chars.get(cfg.account, [])]


def run_tool(login, env=STAGING_ENV, argv=()):
    """Runs main() with the stub login. The password rides on the cfg as in a real run, so a print of cfg.password
    would show here. Returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    cfg = SimpleNamespace(wow_host=env.get("WOW_HOST"), wow_auth_port=env.get("WOW_AUTH_PORT"), account="",
                          password=SENTINEL, verbose_packets=False, dump_packets_dir="")
    with redirect_stdout(out), mock.patch("sys.stderr", err):
        code = sc.main(argv=list(argv), env=env, load=lambda: cfg, login=login)
    return code, out.getvalue(), err.getvalue()


class StagingCharactersTest(unittest.TestCase):
    def test_a_character_less_account_gets_one(self):
        login = StubLogin()
        code, out, err = run_tool(login, argv=["--accounts", "STAGING01"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "created Stagingone\n")
        self.assertEqual(login.sessions["STAGING01"].created, [("Stagingone", 2, 1)])
        self.assertEqual(login.sessions["STAGING01"].logouts, 1)

    def test_the_name_and_class_follow_the_character_rules(self):
        login = StubLogin()
        run_tool(login, argv=["--accounts", "STAGING01..STAGING05"])
        for account, sess in login.sessions.items():
            with self.subTest(account=account):
                ((name, race, class_),) = sess.created
                self.assertTrue(valid_name(name))
                self.assertIn(race, HORDE)
                self.assertIn(class_, HORDE[race][1])

    def test_a_non_empty_account_is_untouched(self):
        login = StubLogin(chars={"STAGING01": [{"name": "Existing", "guid": 7}]})
        code, out, _ = run_tool(login, argv=["--accounts", "STAGING01"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "kept Existing\n")
        self.assertEqual(login.sessions["STAGING01"].created, [])
        self.assertEqual(login.sessions["STAGING01"].logouts, 1)

    def test_the_default_accounts_are_staging01_to_05_in_order(self):
        login = StubLogin(chars={"STAGING02": [{"name": "Kept", "guid": 3}]})
        code, out, _ = run_tool(login)
        self.assertEqual(code, 0)
        self.assertEqual(login.calls, ["STAGING01", "STAGING02", "STAGING03", "STAGING04", "STAGING05"])
        self.assertEqual(out.splitlines(), ["created Stagingone", "kept Kept", "created Stagingthree",
                                            "created Stagingfour", "created Stagingfive"])

    def test_a_failure_is_reported_and_the_other_accounts_still_run(self):
        login = StubLogin(create_fail={"STAGING01": RuntimeError("char create failed for 'Stagingone': "
                                                                 "response code 50")})
        code, out, _ = run_tool(login, argv=["--accounts", "STAGING01,STAGING02"])
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [
            "FAIL STAGING01: RuntimeError: char create failed for 'Stagingone': response code 50",
            "created Stagingtwo"])
        self.assertEqual(login.sessions["STAGING01"].logouts, 1)   # a failed account is logged out too

    def test_the_password_never_reaches_stdout_or_stderr(self):
        # a login that echoes the password into its error: the tool must not print it
        login = StubLogin(fail={"STAGING01": RuntimeError(f"rejected {SENTINEL}")})
        code, out, err = run_tool(login, argv=["--accounts", "STAGING01"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL STAGING01", out)
        self.assertNotIn(SENTINEL, out + err)
        ok_code, ok_out, ok_err = run_tool(StubLogin())
        self.assertEqual(ok_code, 0)
        self.assertNotIn(SENTINEL, ok_out + ok_err)


class ConfigTest(unittest.TestCase):
    def test_the_live_realm_is_refused_before_any_login(self):
        for env in ({**STAGING_ENV, "WOW_HOST": "192.168.1.64"}, {**STAGING_ENV, "WOW_AUTH_PORT": "3724"}):
            with self.subTest(env=env):
                login = StubLogin()
                code, out, err = run_tool(login, env=env)
                self.assertEqual(code, 2)
                self.assertIn("refusing the live realm", err)
                self.assertEqual(login.calls, [])
                self.assertEqual(out, "")

    def test_a_missing_password_or_host_is_refused_before_any_login(self):
        for env, named in (({k: v for k, v in STAGING_ENV.items() if k != "STAGING_ACCOUNT_PASSWORD"},
                            "STAGING_ACCOUNT_PASSWORD is required"),
                           ({k: v for k, v in STAGING_ENV.items() if k != "WOW_HOST"},
                            "WOW_HOST is required")):
            with self.subTest(named=named):
                login = StubLogin()
                code, _, err = run_tool(login, env=env)
                self.assertEqual(code, 2)
                self.assertIn(named, err)
                self.assertEqual(login.calls, [])

    def test_an_account_list_expands_ranges_and_commas(self):
        self.assertEqual(sc.expand_accounts("STAGING01..STAGING03"),
                         ["STAGING01", "STAGING02", "STAGING03"])
        self.assertEqual(sc.expand_accounts("STAGING04,STAGING01"), ["STAGING04", "STAGING01"])


if __name__ == "__main__":
    unittest.main()
