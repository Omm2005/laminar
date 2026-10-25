"""Chat with the lab agent in the terminal: the same agent as the Fetch.ai chat and voice, typed.

    python -m laminar.chat                          # simulated bench, rule parser (no API calls)
    python -m laminar.chat --claude                 # Claude reads free-form requests (needs ANTHROPIC_API_KEY)
    python -m laminar.chat --real --cam 0           # the REAL arm: arm_server.py running, boxes drawn
    python -m laminar.chat --real --check           # only say what the real bench still needs, then exit

Try: "what's on the bench?", "pour 1 ml of blue into well 2", "approve <id>", "runs", "quit".
Every plan is built and checked by code; nothing moves until you approve it.
"""
from __future__ import annotations

import argparse
import getpass
import os

from .hardware import make_system
from .lab_agent import LabAgent, bench_view, claude_interpreter, describe_bench, parse_request


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--real", action="store_true", help="the real arm (default: simulated bench)")
    ap.add_argument("--cam", default="0", help="--real: side camera index")
    ap.add_argument("--claude", action="store_true", help="Claude interprets requests (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--as", dest="me", default=getpass.getuser(), help="your name, as the approver")
    ap.add_argument("--check", action="store_true", help="print the bench and what it still needs, then exit")
    a = ap.parse_args()

    if a.real:
        os.environ["LAMINAR_CAM"] = a.cam
    system = make_system(fake=not a.real, time_scale=0 if not a.real else 1.0)
    try:
        if a.check:
            print(describe_bench(bench_view(system)))
            return 0
        agent = LabAgent(system=system, approvers={a.me},
                         interpret=claude_interpreter() if a.claude else parse_request)
        where = "REAL arm" if a.real else "simulated bench"
        print(f"Laminar lab agent ({where}, {'Claude' if a.claude else 'rule parser'}). "
              f"You are '{a.me}' and can approve. 'quit' to leave.\n")
        print(describe_bench(bench_view(system)) + "\n")
        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if text.lower() in ("quit", "exit", "q"):
                break
            if text:
                print("laminar> " + agent.handle(a.me, text).replace("\n", "\n         ") + "\n")
    finally:
        system.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
