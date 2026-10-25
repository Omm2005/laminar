"""Talk to the Laminar Bench agent from a terminal, the way ASI:One does (Agent Chat Protocol, plain text).

    fetch/.venv/bin/python fetch/chat_cli.py --bench agent1q...                  # type messages; Ctrl+C quits
    fetch/.venv/bin/python fetch/chat_cli.py --bench agent1q... "what's on the bench?" "status"   # scripted

Scripted: each message is sent once the bench has answered the previous one (or after --wait s of quiet).
Settings: CHAT_CLI_SEED (default: a fixed test identity), BENCH_AGENT_ADDRESS, CHAT_CLI_PORT (default 8003).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec

from convex_link import env

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--bench", default=env("BENCH_AGENT_ADDRESS"))
ap.add_argument("--wait", type=float, default=45.0, help="scripted: max seconds to wait for an answer")
ap.add_argument("messages", nargs="*")
a = ap.parse_args()
if not a.bench:
    sys.exit("--bench <Laminar Bench agent address>")

port = int(env("CHAT_CLI_PORT", "8003"))
agent = Agent(name="chat-cli", seed=env("CHAT_CLI_SEED", "laminar chat cli test identity"), port=port,
              endpoint=[f"http://127.0.0.1:{port}/submit"], log_level="WARNING")
chat = Protocol(spec=chat_protocol_spec)
todo = list(a.messages)
last = {"heard": time.time(), "sent": 0.0}


def say(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(type="text", text=text)])


async def send(ctx: Context, text: str):
    print(f"\n> {text}", flush=True)
    last["sent"] = time.time()
    await ctx.send(a.bench, say(text))


@agent.on_interval(period=1.0)
async def tick(ctx: Context):
    if not a.messages:                                   # interactive
        if last["sent"] == 0.0:
            last["sent"] = 1.0
            asyncio.get_running_loop().add_reader(sys.stdin, lambda: asyncio.ensure_future(_line(ctx)))
            print("type a message for the Laminar Bench (Ctrl+C quits)", flush=True)
        return
    waited = time.time() - last["sent"]
    answered = last["heard"] > last["sent"] and time.time() - last["heard"] > 3.0
    if todo and (last["sent"] == 0.0 or answered or waited > a.wait):
        await send(ctx, todo.pop(0))
    elif not todo and (answered or waited > a.wait):
        os._exit(0)


async def _line(ctx: Context):
    line = sys.stdin.readline()
    if line.strip():
        await send(ctx, line.strip())


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    for c in msg.content:
        if isinstance(c, TextContent):
            print(f"\nLaminar Bench: {c.text}", flush=True)
    last["heard"] = time.time()


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(chat)

if __name__ == "__main__":
    agent.run()
