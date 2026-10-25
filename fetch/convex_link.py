"""The bench's dashboard backend (Convex), from the Fetch.ai agents: plain HTTP, no extra packages.

    link = ConvexLink()                       # URL from LAMINAR_CONVEX_URL, else console/.env.local
    link.mutation("remote:ask", {...})
    link.query("remote:runs", {...})

Also env(): settings from the environment, else from a KEY=value line in the repo's .env (or ~/laminar/.env).
Both files are git-ignored: agent seeds and keys never go in the repo.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILES = [REPO / ".env", Path.home() / "laminar" / ".env"]


def env(name: str, default: str | None = None) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    for f in ENV_FILES:
        if f.exists():
            for line in f.read_text().splitlines():
                m = re.match(rf"^\s*{re.escape(name)}\s*=\s*(.*)$", line)
                if m and m.group(1).strip().strip("\"'"):
                    return m.group(1).strip().strip("\"'")
    return default


def convex_url() -> str:
    url = env("LAMINAR_CONVEX_URL")
    if url:
        return url.rstrip("/")
    local = REPO / "console" / ".env.local"
    if local.exists():
        m = re.search(r"^\s*VITE_CONVEX_URL\s*=\s*(\S+)", local.read_text(), re.M)
        if m:
            return m.group(1).strip("\"'").rstrip("/")
    raise SystemExit("no dashboard backend: start it (console/start.sh) or set LAMINAR_CONVEX_URL")


class ConvexError(RuntimeError):
    """A Convex function threw: the message is meant for people (e.g. "only the technician can approve")."""


class ConvexLink:
    def __init__(self, url: str | None = None, timeout: float = 15.0):
        self.url, self.timeout = url or convex_url(), timeout

    def _call(self, kind: str, path: str, args: dict):
        body = json.dumps({"path": path, "args": args, "format": "json"}).encode()
        req = urllib.request.Request(f"{self.url}/api/{kind}", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:     # Convex returns function errors as 4xx/5xx with a JSON body
            out = json.loads(e.read() or b"{}")
        if out.get("status") != "success":
            raise ConvexError(_clean(out.get("errorMessage", "dashboard error")))
        return out["value"]

    def query(self, path: str, args: dict | None = None):
        return self._call("query", path, args or {})

    def mutation(self, path: str, args: dict | None = None):
        return self._call("mutation", path, args or {})


def _clean(msg: str) -> str:
    """Convex wraps thrown errors ("[Request ID: ..] Server Error\\nUncaught Error: <msg>\\n    at ..."): keep <msg>."""
    m = re.search(r"Uncaught Error: (.*?)(?:\n\s+at |\Z)", msg, re.S)
    return (m.group(1) if m else msg).strip()
