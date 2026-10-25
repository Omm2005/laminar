"""Give the running Laminar Bench agent its Agentverse mailbox (so ASI:One can reach it), without the web inspector.

The usual way is the agent inspector link the agent prints at start ("Connect" > "Mailbox"). That page needs the
browser to reach the agent on this computer; some browsers block it. This does the same from here:

    1. agentverse.ai > profile > API Keys > create a key
    2. put AGENTVERSE_API_KEY=<the key> in ~/laminar/.env (git-ignored; the key is never printed)
    3. with bench_agent.py running:   fetch/.venv/bin/python fetch/connect_mailbox.py
    4. restart bench_agent.py: it logs "Mailbox access token acquired" and shows up under My Agents on Agentverse
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from convex_link import env

port = env("BENCH_AGENT_PORT", "8001")
key = env("AGENTVERSE_API_KEY")
if not key:
    sys.exit("put AGENTVERSE_API_KEY=<key from agentverse.ai > API Keys> in ~/laminar/.env first")
req = urllib.request.Request(f"http://127.0.0.1:{port}/connect", headers={"Content-Type": "application/json"},
                             data=json.dumps({"user_token": key, "agent_type": "mailbox"}).encode())
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read())
except urllib.error.URLError as e:
    sys.exit(f"bench_agent.py is not running on port {port} ({e}): start it first")
print("mailbox connected: restart bench_agent.py" if out.get("success") else f"Agentverse said: {out.get('detail', out)}")
