"""Fetch.ai chat agent for Laminar: reachable from ASI:One through Agentverse (mailbox), speaks the Agent
Chat Protocol, and runs the whole workflow in the chat: look at the bench, propose a plan, check it, run it
on the executor after an approval, verify the pour and report. The logic is lab_agent.LabAgent.

    pip install -r requirements-agents.txt
    export LAMINAR_AGENT_SEED="<any long secret phrase>"      # fixes the agent's address across restarts
    export LAMINAR_APPROVERS="<your ASI:One user address>"    # who may approve runs (comma-separated)
    python -m laminar.fetch_agent                              # prints the agent address + inspector link

Then open the inspector link, Connect -> Mailbox, and the agent is on Agentverse; search it in ASI:One.

Entry agent: set LAMINAR_PLANNER_AGENT to the planner + orchestrator agent's address (planner_agent.py, or
Om's) and this agent forwards bench queries, plans and approved runs to it over the LaminarPlanner protocol
(agent_protocol.py), keeping the chat, the safety checks and the approvals here. Without it, or if it does
not answer, the built-in planner and executor answer instead.

Optional: TIGER_DATABASE_URL streams every event to Tiger Data; LAMINAR_PLANNER=claude interprets
free-form requests with Claude (default: rule parser, no API calls).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

from . import tiger
from .agent_protocol import BenchQuery, BenchView, PlanProposal, PlanRequest, RunReport, RunRequest
from .lab_agent import HELP, LabAgent

PLANNER_ENV = "LAMINAR_PLANNER_AGENT"


class RemotePlanner:
    """lab_agent.Remote over uAgents messages. LabAgent runs in a worker thread, so each call is scheduled on
    the agent's event loop and waited for; any failure raises and LabAgent falls back to its built-in path."""

    def __init__(self, address: str, quick_s: int = 20, run_s: int = 600):
        self.address, self.quick_s, self.run_s = address, quick_s, run_s
        self.ctx: Context | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def _ask(self, msg, reply_type, timeout: int):
        fut = asyncio.run_coroutine_threadsafe(
            self.ctx.send_and_receive(self.address, msg, response_type=reply_type, timeout=timeout), self.loop)
        reply, status = fut.result(timeout + 5)
        if not isinstance(reply, reply_type):
            raise TimeoutError(f"planner agent: no {reply_type.__name__} ({status})")
        return reply

    def bench(self) -> dict:
        return self._ask(BenchQuery(), BenchView, self.quick_s).view

    def plan(self, text: str, view: dict, requester: str):
        p = self._ask(PlanRequest(request_id=uuid4().hex[:8], text=text, bench=view, requester=requester),
                      PlanProposal, self.quick_s)
        return p.plan, p.error

    def run(self, plan: dict, approved_by: str) -> dict:
        return self._ask(RunRequest(plan=plan, approved_by=approved_by), RunReport, self.run_s).report


README = Path(__file__).resolve().parents[1] / "AGENT.md"


def reply(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(type="text", text=text)])


def make_agent(lab: LabAgent | None = None) -> Agent:
    seed = os.environ.get("LAMINAR_AGENT_SEED")
    if not seed:
        sys.exit("set LAMINAR_AGENT_SEED (any long secret phrase) so the agent keeps the same address")
    agent = Agent(name="laminar-lab", seed=seed, port=int(os.environ.get("LAMINAR_AGENT_PORT", "8001")),
                  mailbox=True, readme_path=str(README) if README.exists() else None,
                  description="Lab robot arm that pours measured volumes, checks them with a camera, and reports.",
                  publish_agent_details=True)
    lab = lab or LabAgent()
    if lab.remote is None and os.environ.get(PLANNER_ENV):
        lab.remote = RemotePlanner(os.environ[PLANNER_ENV])
    chat = Protocol(spec=chat_protocol_spec)

    @agent.on_event("startup")
    async def startup(ctx: Context):
        ctx.logger.info(f"Laminar agent address: {agent.address}")
        ctx.logger.info(f"approvers: {sorted(lab.approvers) or ('anyone (simulated bench)' if lab.simulated else 'none')}")
        ctx.logger.info(f"planner agent: {getattr(lab.remote, 'address', None) or 'built-in planner only'}")

    @chat.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
        for item in msg.content:
            if isinstance(item, StartSessionContent):
                await ctx.send(sender, reply(HELP))
            elif isinstance(item, TextContent):
                # a run blocks while the arm moves: keep it off the event loop
                if isinstance(lab.remote, RemotePlanner):
                    lab.remote.ctx, lab.remote.loop = ctx, asyncio.get_running_loop()
                text = await asyncio.to_thread(lab.handle, sender, item.text)
                await ctx.send(sender, reply(text))
            elif isinstance(item, EndSessionContent):
                ctx.logger.info(f"session with {sender} ended")

    @chat.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        pass

    agent.include(chat, publish_manifest=True)
    return agent


def main() -> None:
    sink = tiger.install()
    if sink:
        print("streaming telemetry to Tiger Data")
    make_agent().run()


if __name__ == "__main__":
    main()
