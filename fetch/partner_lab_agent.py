"""Partner lab QC agent: another lab's agent that uses the Laminar Bench agent to run a dispensing QC series, then
acts on the measured results. Agent-to-agent over the Fetch.ai Agent Chat Protocol, no person in its loop. (On the
real arm the bench's technician still approves each plan.)

    fetch/.venv/bin/python fetch/partner_lab_agent.py --bench agent1q...          # the Laminar Bench agent's address
    fetch/.venv/bin/python fetch/partner_lab_agent.py --bench agent1q... --series 0.5:1,1:2,1.5:3 --liquid "blue dye"

What it does:
  1. asks the bench for the series (mL:container pairs), introducing itself with MetadataContent {"lab": ...}
  2. plan_waiting: on a simulated bench it approves its own plan (--no-approve to leave it); on the real arm it
     waits for the bench's technician
  3. run_finished: checks every container against its QC spec (target +- --tolerance mL). Under-filled containers
     get ONE top-up request for the missing volume; over-filled ones are flagged (liquid can't be taken back out)
  4. writes the QC report to fetch/reports/qc_<time>.json and prints PASS / FAIL per container

Settings: PARTNER_AGENT_SEED (or --seed), BENCH_AGENT_ADDRESS (or --bench), PARTNER_AGENT_PORT (default 8002).
--mailbox if this agent runs on a different machine than the bench (it then needs an Agentverse mailbox).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    TextContent,
    chat_protocol_spec,
)

from convex_link import env

HERE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--bench", default=env("BENCH_AGENT_ADDRESS"), help="the Laminar Bench agent's address (agent1q...)")
ap.add_argument("--series", default="0.5:1,1.0:2,1.5:3", help="mL:container pairs")
ap.add_argument("--liquid", default="blue dye", help="which liquid (as the bench names it)")
ap.add_argument("--tolerance", type=float, default=0.15, help="QC spec: |measured - target| <= this, mL")
ap.add_argument("--lab", default="Partner lab QC agent", help="how this lab is named on the bench's dashboard")
ap.add_argument("--no-approve", action="store_true", help="never approve, even on a simulated bench")
ap.add_argument("--seed", default=env("PARTNER_AGENT_SEED"))
ap.add_argument("--mailbox", action="store_true", help="receive replies through an Agentverse mailbox")
ap.add_argument("--timeout", type=float, default=900.0, help="give up after this many seconds")
a = ap.parse_args()
if not a.bench:
    raise SystemExit("--bench <Laminar Bench agent address> (or BENCH_AGENT_ADDRESS in .env)")
if not a.seed:
    raise SystemExit("set PARTNER_AGENT_SEED (a long secret string) in the environment or .env, or pass --seed")

SERIES = [(float(ml), int(c)) for ml, c in (p.split(":") for p in a.series.split(","))]
port = int(env("PARTNER_AGENT_PORT", "8002"))
agent = Agent(name="Partner lab QC", seed=a.seed, port=port, mailbox=a.mailbox,
              endpoint=None if a.mailbox else [f"http://127.0.0.1:{port}/submit"],
              description="QC agent of a partner lab: runs dispensing QC series on the Laminar Bench robot agent.")
chat = Protocol(spec=chat_protocol_spec)

# what this QC run knows so far
state = {"sent": False, "started": time.time(), "retested": False, "plans": {}, "spec": {}, "measured": {},
         "done": False, "asked": None}     # asked: the request still waiting for a plan ("series" / "top-up")


def message(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                       content=[TextContent(type="text", text=text),
                                MetadataContent(type="metadata", metadata={"lab": a.lab})])


def request_text(series: list[tuple[float, int]], what: str) -> str:
    pours = ", ".join(f"{ml:g} mL into container {c}" for ml, c in series)
    return f"{what}: pour {a.liquid}, {pours}. Please report the measured volume in each container."


@agent.on_interval(period=2.0)
async def kick(ctx: Context):
    if not state["sent"]:
        state["sent"] = True
        for ml, c in SERIES:
            state["spec"][c] = state["spec"].get(c, 0.0) + ml
        text = request_text(SERIES, "Dispensing QC series")
        state["asked"] = "series"
        ctx.logger.info(f"-> bench: {text}")
        await ctx.send(a.bench, message(text))
    elif not state["done"] and time.time() - state["started"] > a.timeout:
        ctx.logger.error("QC run timed out waiting for the bench")
        finish(ctx, timed_out=True)


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    if sender != a.bench:
        return
    for c in msg.content:
        if isinstance(c, TextContent):
            ctx.logger.info("<- bench: " + c.text.replace("\n", "\n            "))
    events = []
    for c in msg.content:
        if isinstance(c, MetadataContent) and "laminar_events" in c.metadata:
            events += json.loads(c.metadata["laminar_events"])
    for ev in events:
        await on_event(ctx, ev)
    if not events and state["asked"] and not state["done"]:
        # the bench answered a request without a plan: it declined (not enough liquid, would overflow, too small...)
        why = " ".join(c.text for c in msg.content if isinstance(c, TextContent))[:500]
        ctx.logger.warning(f"the bench did not plan the {state['asked']} (its reason is above)")
        state["declined"] = {"request": state["asked"], "reason": why}
        finish(ctx)


async def on_event(ctx: Context, ev: dict):
    kind, pid = ev.get("laminar_event"), ev.get("plan_id", "")
    if kind == "plan_waiting":
        state["asked"] = None
        state["plans"][pid] = "waiting"
        if ev.get("simulated") and not a.no_approve:
            ctx.logger.info(f"plan {pid[-6:]} is on a simulated bench: approving it")
            await ctx.send(a.bench, message("approve"))
        else:
            ctx.logger.info(f"plan {pid[-6:]} waits for the bench technician")
    elif kind in ("approved", "rejected"):
        state["plans"][pid] = kind
    elif kind == "run_finished":
        state["plans"][pid] = ev.get("status", "")
        for r in ev.get("results") or []:
            if r.get("container") is not None:
                c = int(r["container"])
                state["measured"][c] = state["measured"].get(c, 0.0) + float(r["measured_ml"])
        await decide(ctx)


async def decide(ctx: Context):
    """After a run: top up under-filled containers once, then write the report."""
    short = {c: round(spec - state["measured"].get(c, 0.0), 2) for c, spec in state["spec"].items()}
    under = [(ml, c) for c, ml in short.items() if ml > a.tolerance]
    if under and not state["retested"]:
        state["retested"] = True
        text = request_text(under, "QC top-up (these came out under spec)")
        state["asked"] = "top-up"
        ctx.logger.info(f"-> bench: {text}")
        await ctx.send(a.bench, message(text))
        return
    finish(ctx)


def finish(ctx: Context, timed_out: bool = False):
    state["done"] = True
    rows = []
    for c, spec in sorted(state["spec"].items()):
        got = state["measured"].get(c)
        ok = got is not None and abs(got - spec) <= a.tolerance
        rows.append({"container": c, "target_ml": spec, "measured_ml": None if got is None else round(got, 3),
                     "error_ml": None if got is None else round(got - spec, 3), "pass": ok})
        ctx.logger.info(f"QC container {c}: target {spec:g} mL, measured "
                        f"{'—' if got is None else f'{got:.2f}'} mL -> {'PASS' if ok else 'FAIL'}")
    report = {"lab": a.lab, "bench": a.bench, "liquid": a.liquid, "tolerance_ml": a.tolerance,
              "timed_out": timed_out, "declined": state.get("declined"), "retested": state["retested"], "plans": state["plans"], "containers": rows,
              "pass": not timed_out and not state.get("declined") and all(r["pass"] for r in rows), "finished": datetime.now(timezone.utc).isoformat()}
    out = HERE / "reports" / f"qc_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    ctx.logger.info(f"QC {'PASSED' if report['pass'] else 'FAILED'}: report {out}")
    asyncio.get_running_loop().call_later(2.0, os._exit, 0 if report["pass"] else 1)   # done: leave


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(chat, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
