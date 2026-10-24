"""Fetch.ai chat agent for Laminar: reachable from ASI:One through Agentverse (mailbox), speaks the Agent
Chat Protocol, and runs the whole workflow in the chat: look at the bench, propose a plan, check it, run it
on the executor after an approval, verify the pour and report. The logic is lab_agent.LabAgent.

    pip install -r requirements-agents.txt
    export LAMINAR_AGENT_SEED="<any long secret phrase>"      # fixes the agent's address across restarts
    export LAMINAR_APPROVERS="<your ASI:One user address>"    # who may approve runs (comma-separated)
    python -m laminar.fetch_agent                              # prints the agent address + inspector link

Then open the inspector link, Connect -> Mailbox, and the agent is on Agentverse; search it in ASI:One.
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
from .lab_agent import HELP, LabAgent

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
    chat = Protocol(spec=chat_protocol_spec)

    @agent.on_event("startup")
    async def startup(ctx: Context):
        ctx.logger.info(f"Laminar agent address: {agent.address}")
        ctx.logger.info(f"approvers: {sorted(lab.approvers) or ('anyone (simulated bench)' if lab.simulated else 'none')}")

    @chat.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
        for item in msg.content:
            if isinstance(item, StartSessionContent):
                await ctx.send(sender, reply(HELP))
            elif isinstance(item, TextContent):
                # a run blocks while the arm moves: keep it off the event loop
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
