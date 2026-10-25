"""Laminar Bench agent: other labs ask this lab's robot arm to run liquid-handling tests, from ASI:One or from their
own agent, over the Fetch.ai Agent Chat Protocol.

    fetch/.venv/bin/python fetch/bench_agent.py

    other lab (ASI:One chat, or its agent) ──ChatMessage──► this agent ──► dashboard backend (Convex, remote.ts)
         "pour 1 mL of blue dye into container 2"                          │  the Lab assistant (Claude + tools:
                                                                            │  bench state, plan checks, approval)
         ◄── plan + "waiting for the technician" ───────────────────────────┤
         ◄── "approved by Om, the robot is starting" ◄── a person approves on the Lab page (real arm)
         ◄── "done: camera measured 0.98 mL (-0.02)" ◄── arm + camera run the plan (console/bridge/run_plan.py)

The agent only relays: every request is planned and checked by the same assistant and code as the Lab page, and on
the real arm nothing moves until the technician approves. On a SIMULATED bench (run_plan.py --sim) the requester may
reply "approve" itself, so anyone can run the whole loop from ASI:One.

Commands in the chat (anything else goes to the assistant): "approve" (simulated bench), "cancel", "status".
Agents (not people) can send a MetadataContent {"lab": "<name>"}: they are named that way on the dashboard and also
get machine-readable updates (MetadataContent "laminar_event": plan_waiting | approved | run_finished, see
partner_lab_agent.py).

Settings (environment, or KEY=value lines in the repo's .env / ~/laminar/.env, git-ignored):
    BENCH_AGENT_SEED     the agent's identity (any long secret string; keep it: the address comes from it)
    LAMINAR_CONVEX_URL   the dashboard backend (default: console/.env.local, written by `bunx convex dev`)
    BENCH_AGENT_PORT     default 8001
    BENCH_AGENT_MAILBOX  1 (default): reachable through its Agentverse mailbox (ASI:One); 0: local endpoint only
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

from convex_link import ConvexError, ConvexLink, env

HERE = Path(__file__).resolve().parent
OPEN = ("awaiting_approval", "approved", "running", "pouring")
WATCH_IDLE_S = 30 * 60          # keep relaying a requester's updates this long after its last open plan
APPROVE = re.compile(r"^\s*(approve|approved|yes|yes,? run it|run it|go ahead|confirm)\b", re.I)
CANCEL = re.compile(r"^\s*(cancel|reject|stop the plan|don'?t run it)\b", re.I)
STATUS = re.compile(r"^\s*(status|progress|results?|how did it go|is it done)\b", re.I)

seed = env("BENCH_AGENT_SEED")
if not seed:
    raise SystemExit("set BENCH_AGENT_SEED (a long secret string) in the environment or in .env")

port = int(env("BENCH_AGENT_PORT", "8001"))
mailbox = env("BENCH_AGENT_MAILBOX", "1") != "0"     # 0: local endpoint only (testing on one machine, no Agentverse)
agent = Agent(
    name="Laminar Bench",
    seed=seed,
    port=port,
    mailbox=mailbox,
    endpoint=None if mailbox else [f"http://127.0.0.1:{port}/submit"],
    readme_path=str(HERE / "AGENT_README.md"),
    description="Remote wet-lab bench: a camera-checked robot arm pours measured volumes of liquid into containers "
                "for other labs. Ask in plain words; a person at the bench approves; you get the measured volumes.",
    publish_agent_details=True,
)
chat = Protocol(spec=chat_protocol_spec)
link = ConvexLink()


# ------------------------------------------------------------------ helpers

def _msg(text: str, meta: dict | None = None, end: bool = False) -> ChatMessage:
    content: list = [TextContent(type="text", text=text)]
    if meta:
        content.append(MetadataContent(type="metadata", metadata={k: v if isinstance(v, str) else json.dumps(v)
                                                                   for k, v in meta.items()}))
    if end:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content)


async def call(kind: str, path: str, args: dict):
    fn = link.query if kind == "query" else link.mutation
    return await asyncio.to_thread(fn, path, args)


def _state(ctx: Context, sender: str) -> dict:
    return ctx.storage.get(f"s:{sender}") or {}


def _save(ctx: Context, sender: str, st: dict) -> None:
    ctx.storage.set(f"s:{sender}", st)
    watch = set(ctx.storage.get("watch") or [])
    watch.add(sender)
    ctx.storage.set("watch", sorted(watch))


def _short(plan_id: str) -> str:
    return plan_id[-6:]


def _results_lines(run: dict) -> list[str]:
    out = []
    for r in run.get("results") or []:
        where = f"container {int(r['container'])}" if r.get("container") is not None else "container"
        bits = [f"measured **{r['measured_ml']:.2f} mL**"]
        if r.get("target_ml") is not None:
            bits.append(f"target {r['target_ml']:g} mL")
        if isinstance(r.get("error_ml"), (int, float)):
            bits.append(f"off by {r['error_ml']:+.2f} mL")
        out.append(f"- {where}: " + ", ".join(bits) + (" (within tolerance)" if r.get("ok") else " (OUT of tolerance)"))
    return out


async def _open_plan(sender: str) -> dict | None:
    runs = await call("query", "remote:runs", {"sender": sender, "limit": 5})
    return next((r for r in runs if r["status"] == "awaiting_approval"), None)


# ------------------------------------------------------------------ relaying the bench's replies

async def render(sender: str, rows: list[dict]) -> tuple[str, list[dict]]:
    """The thread's new rows as one chat message (markdown) + machine-readable events for agent requesters."""
    parts, events = [], []
    bench = None
    for r in rows:
        tool, args = r.get("tool"), r.get("args") or {}
        if tool == "plan":
            bench = bench or await call("query", "remote:bench", {})
            pid = args.get("jobId", "")
            lines = [f"**Plan {_short(pid)}: waiting for approval**"]
            for p in args.get("pours") or []:
                lines.append(f"- {p['ml']:g} mL of {p['vialName']} (vial {p['vialLetter']}) into {p['containerName']} "
                             f"({p['containerMlNow']:g} to {p['containerMlNow'] + p['ml']:.2f} of {p['capacityMl']:g} mL)")
            if not args.get("pours"):
                lines += [f"- {s['label']}" for s in args.get("steps") or []]
            hand = [s["label"] for s in args.get("steps") or [] if s.get("placeholder")]
            if hand:
                lines.append(f"_{len(hand)} step(s) are done by hand by the operator (grab not recorded yet)._")
            lines.append("This bench is **simulated**: reply **approve** to run it, or **cancel**."
                         if bench.get("simulated") else
                         "The technician at the bench approves it on the robot's dashboard; I'll message you when it "
                         "runs. Reply **cancel** to withdraw it.")
            parts.append("\n".join(lines))
            events.append({"laminar_event": "plan_waiting", "plan_id": pid, "simulated": bool(bench.get("simulated")),
                           "pours": args.get("pours") or []})
        elif tool == "approval":
            ok = bool(args.get("approved"))
            parts.append(r["text"])
            events.append({"laminar_event": "approved" if ok else "rejected", "plan_id": args.get("jobId", ""),
                           "by": args.get("by", "")})
        elif tool == "run_summary":
            runs = await call("query", "remote:runs", {"sender": sender, "limit": 10})
            run = next((x for x in runs if x["plan_id"] == args.get("jobId")), None)
            head = "**Run finished**" if args.get("status") == "done" else "**Run stopped**"
            lines = [head, r["text"], *(_results_lines(run) if run else [])]
            lines.append("_Every step, approval and camera reading is kept in the bench's audit log._")
            parts.append("\n".join(lines))
            events.append({"laminar_event": "run_finished", "plan_id": args.get("jobId", ""),
                           "status": args.get("status", ""), "results": (run or {}).get("results") or []})
        elif tool in (None, "") and r.get("text"):
            parts.append(r["text"])
    return "\n\n".join(parts), events


@agent.on_interval(period=1.5)
async def relay(ctx: Context):
    """Send each watched requester what the bench said since last time (replies, plan, approval, results)."""
    now = time.time()
    keep = []
    for sender in ctx.storage.get("watch") or []:
        st = _state(ctx, sender)
        try:
            rows = await call("query", "remote:replies", {"sender": sender, "since": st.get("since", 0)})
            if rows:
                text, events = await render(sender, rows)
                st["since"] = rows[-1]["at"]
                if text:
                    meta = None
                    if st.get("machine"):
                        meta = {"laminar_events": events}
                    await ctx.send(sender, _msg(text, meta))
                st["active"] = now
            runs = await call("query", "remote:runs", {"sender": sender, "limit": 3})
            run = runs[0] if runs else None
            if run and run["status"] in ("running", "pouring") and st.get("announced") != run["plan_id"]:
                st["announced"] = run["plan_id"]                     # one "it's moving" per plan
                await ctx.send(sender, _msg(f"The robot is running plan {_short(run['plan_id'])} now."))
            if any(r["status"] in OPEN for r in runs):
                st["active"] = now
        except ConvexError as e:
            ctx.logger.warning(f"relay to {sender[:16]}: {e}")
        except OSError as e:                                         # backend down: try again next tick
            ctx.logger.warning(f"dashboard backend unreachable: {e}")
            keep.append(sender)
            continue
        _save(ctx, sender, st)
        if now - st.get("active", now) < WATCH_IDLE_S:
            keep.append(sender)
    ctx.storage.set("watch", keep)


# ------------------------------------------------------------------ incoming chat

@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    meta = {}
    for c in msg.content:
        if isinstance(c, MetadataContent):
            meta.update(c.metadata)
    st = _state(ctx, sender)
    if meta.get("lab"):
        st.update(label=meta["lab"][:60], via="agent", machine=True)
    st.setdefault("label", "ASI:One user")
    st.setdefault("via", "asi-one")
    st.setdefault("since", int(time.time() * 1000) - 1000)
    st["active"] = time.time()
    _save(ctx, sender, st)
    if not text:
        if any(isinstance(c, StartSessionContent) for c in msg.content):
            ctx.logger.info(f"session started by {sender[:16]}")
        return
    ctx.logger.info(f"{st['label']} ({sender[:16]}): {text[:120]}")
    try:
        if APPROVE.match(text) and (plan := await _open_plan(sender)):
            await call("mutation", "remote:approve", {"sender": sender, "jobId": plan["plan_id"]})
            return                                                   # the approval message comes through relay()
        if CANCEL.match(text) and (plan := await _open_plan(sender)):
            await call("mutation", "remote:cancel", {"sender": sender, "jobId": plan["plan_id"]})
            return
        if STATUS.match(text) and len(text) < 40:
            runs = await call("query", "remote:runs", {"sender": sender, "limit": 3})
            if not runs:
                await ctx.send(sender, _msg("You have no runs on this bench yet. Ask for a pour, e.g. "
                                            "\"pour 1 mL of the blue dye into container 2\"."))
            else:
                lines = []
                for r in runs:
                    lines.append(f"**Plan {_short(r['plan_id'])}** ({r['request'][:80]}): {r['status'].replace('_', ' ')}"
                                 + (f", approved by {r['approved_by']}" if r.get("approved_by") else "")
                                 + (f". {r['fail_reason']}" if r.get("fail_reason") else ""))
                    lines += _results_lines(r)
                await ctx.send(sender, _msg("\n".join(lines)))
            return
        await call("mutation", "remote:ask", {"sender": sender, "text": text, "label": st["label"], "via": st["via"]})
        # the assistant's reply arrives in the thread; relay() sends it on
    except ConvexError as e:
        await ctx.send(sender, _msg(str(e)[:1].upper() + str(e)[1:]))
    except OSError:
        await ctx.send(sender, _msg("The bench is offline right now (its dashboard isn't reachable). Try again later."))


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(chat, publish_manifest=True)


@agent.on_event("startup")
async def hello(ctx: Context):
    try:
        b = link.query("remote:bench", {})
        state = ("SIMULATED bench" if b["simulated"] else "REAL arm") if b["online"] else "bench link OFFLINE"
        ctx.logger.info(f"dashboard {link.url}: {state}" + (f", not ready: {b['not_ready']}" if b.get("not_ready") else ""))
    except Exception as e:
        ctx.logger.warning(f"dashboard backend {link.url} not reachable yet: {e}")
    ctx.logger.info(f"Laminar Bench agent address: {agent.address}")


if __name__ == "__main__":
    agent.run()
