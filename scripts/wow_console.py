#!/usr/bin/env python3
"""Send commands to the containerized TrinityCore worldserver console over its socket.io API.

Use instead of `docker attach` when you have no TTY (scripts, automation, remote agents).
The image's web UI exposes the worldserver stdin as the socket.io event `worldserver_input`;
this drives engine.io polling directly — no extra dependencies beyond the stdlib.

Usage:
    python3 wow_console.py '<COMMAND>' ['<COMMAND>' ...]

Examples:
    python3 wow_console.py 'account create bob secret'
    python3 wow_console.py 'account create bob secret' 'account set gmlevel bob 3 -1'
    python3 wow_console.py 'server info'

Set WOW_HOST to override the default host (default: 192.168.1.64).
Requires the web UI reachable (port 3000). Liveness proof: the worldserver log ends
with `worldserver-daemon) ready...` followed by a `TC>` prompt.
"""
import json
import os
import re
import sys
import time
import urllib.request

HOST = os.environ.get("WOW_HOST", "192.168.1.64")
PORT = os.environ.get("WOW_PORT", "3000")
BASE = f"http://{HOST}:{PORT}/socket.io/"


def post(sid, payload):
    url = f"{BASE}?EIO=4&transport=polling&sid={sid}"
    req = urllib.request.Request(url, data=payload.encode(), method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()


def get(sid):
    url = f"{BASE}?EIO=4&transport=polling&sid={sid}&t={int(time.time() * 1000)}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read().decode()


def main(commands):
    if not commands:
        print(__doc__)
        return 1

    # 1. engine.io handshake
    with urllib.request.urlopen(
        f"{BASE}?EIO=4&transport=polling&t={int(time.time() * 1000)}", timeout=10
    ) as r:
        hs = r.read().decode()
    sid = json.loads(hs[hs.find("{"):])["sid"]
    print(f"engine.io sid: {sid}")

    # 2. join the default socket.io namespace
    post(sid, "40")
    time.sleep(0.5)
    get(sid)  # drain the initial state burst

    # 3. send each console command
    for cmd in commands:
        post(sid, "42" + json.dumps(["worldserver_input", cmd]))
        print(f"-> {cmd}")
        time.sleep(2.0)

    # 4. read the console echo back
    time.sleep(2.0)
    try:
        out = get(sid)
        for m in re.findall(r'"output":"((?:[^"\\]|\\.)*)"', out):
            print("OUT:", json.loads('"' + m + '"')[-400:])
    except Exception as e:  # noqa: BLE001
        print("poll err:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
