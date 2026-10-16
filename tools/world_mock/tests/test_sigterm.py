"""#401 end to end: SIGTERM to a real `python -m agent` subprocess makes it send
CMSG_LOGOUT_REQUEST to the world_mock and exit cleanly (what `docker stop` does)."""

import glob
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
from tools.world_mock import server  # noqa: E402

from agent import opcodes as op  # noqa: E402

REPO = ROOT.parents[1]
EXIT_BY_S = 8.0  # below Docker's 10 s stop grace


class SigtermLogoutTest(unittest.TestCase):
    def test_sigterm_sends_logout_request_and_exits_zero(self):
        mock = server.WorldMock()
        self.addCleanup(mock.close)
        audit = tempfile.TemporaryDirectory()
        self.addCleanup(audit.cleanup)
        env = {**os.environ, "WOW_HOST": "127.0.0.1", "WOW_AUTH_PORT": str(mock.auth_port),
               "WOW_ACCOUNT": server.DEFAULT_ACCOUNT, "WOW_PASSWORD": server.DEFAULT_PASSWORD,
               "WOW_CHARACTER": "Mock", "AGENT_CHANNELS": "none", "AGENT_AUDIT_DIR": audit.name,
               "AGENT_HTTP_PORT": "0", "AGENT_THINK_INTERVAL_S": "30", "LLM_BASE_URL": "",
               "PYTHONPATH": str(REPO)}
        proc = subprocess.Popen([sys.executable, "-m", "agent"], env=env, cwd=REPO,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.kill)
        end = time.monotonic() + 10
        while mock.world_connections < 1 and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(mock.world_connections, 1, "agent never reached the world")
        time.sleep(1.0)  # let the login finish and the think loop enter its sleep
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=EXIT_BY_S)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(len(mock.packets_received(op.CMSG_LOGOUT_REQUEST)), 1, out)
        # #292: the same run, as the lifecycle lines the supervisor left in the audit directory
        (path,) = glob.glob(os.path.join(audit.name, "*", "lifecycle", "*.jsonl"))
        with open(path) as f:
            walk = [(d["from"], d["to"], d["reason"]) for d in map(json.loads, f)]
        self.assertEqual(walk, [("connecting", "online", "connected"), ("online", "stopping", "stop"),
                                ("stopping", "stopped", "stop")], out)


if __name__ == "__main__":
    unittest.main()
