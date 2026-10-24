"""Reference planner + orchestrator agent behind laminar-lab: answers the LaminarPlanner protocol
(agent_protocol.py) with the built-in rule planner and the executor. Om's Claude planner replaces plan_for().

    export LAMINAR_PLANNER_SEED="<long secret phrase>"     # fixes this agent's address
    python -m laminar.planner_agent                          # prints its address
    # then give that address to the entry agent: LAMINAR_PLANNER_AGENT=agent1q... python -m laminar.fetch_agent
"""
from __future__ import annotations

import asyncio
import os
import sys

from uagents import Agent, Context

from .agent_protocol import (BenchQuery, BenchView, PlanProposal, PlanRequest, RunReport, RunRequest,
                             planner_protocol)
from .executor import Executor
from .hardware import System, make_system
from .lab_agent import bench_view, build_plan, parse_request, validate_plan


class Orchestrator:
    """The planner + executor this agent wraps. Swap plan_for() for a Claude planner; keep the shapes."""

    def __init__(self, system: System | None = None):
        self.system = system or make_system(time_scale=0)

    def bench(self) -> dict:
        return bench_view(self.system)

    def plan_for(self, text: str) -> tuple[dict | None, str]:
        view = self.bench()
        intent = parse_request(text, view)
        if intent.action != "plan":
            return None, intent.note or "I can plan pours, e.g. 'pour 2 mL of blue into tube1'."
        return build_plan(intent, view, text)

    def run(self, plan: dict, approved_by: str) -> dict:
        ex = Executor(self.system, validator=lambda p, bench: validate_plan(p, self.bench()))
        return ex.run(plan, approved_by=approved_by).to_dict()


def make_agent(orch: Orchestrator | None = None) -> Agent:
    seed = os.environ.get("LAMINAR_PLANNER_SEED")
    if not seed:
        sys.exit("set LAMINAR_PLANNER_SEED (any long secret phrase) so the planner keeps the same address")
    port = int(os.environ.get("LAMINAR_PLANNER_PORT", "8002"))
    agent = Agent(name="laminar-planner", seed=seed, port=port, endpoint=[f"http://127.0.0.1:{port}/submit"],
                  description="Laminar planner + orchestrator: lab request -> checked plan -> executor run.")
    orch = orch or Orchestrator()
    proto = planner_protocol

    @agent.on_event("startup")
    async def startup(ctx: Context):
        ctx.logger.info(f"Laminar planner address: {agent.address}")

    @proto.on_message(BenchQuery, replies=BenchView)
    async def on_bench(ctx: Context, sender: str, msg: BenchQuery):
        await ctx.send(sender, BenchView(view=orch.bench()))

    @proto.on_message(PlanRequest, replies=PlanProposal)
    async def on_plan(ctx: Context, sender: str, msg: PlanRequest):
        plan, err = await asyncio.to_thread(orch.plan_for, msg.text)
        await ctx.send(sender, PlanProposal(request_id=msg.request_id, plan=plan, error=err))

    @proto.on_message(RunRequest, replies=RunReport)
    async def on_run(ctx: Context, sender: str, msg: RunRequest):
        report = await asyncio.to_thread(orch.run, msg.plan, msg.approved_by)   # blocks while the arm moves
        await ctx.send(sender, RunReport(report=report))

    agent.include(proto, publish_manifest=True)
    return agent


def main() -> None:
    make_agent().run()


if __name__ == "__main__":
    main()
