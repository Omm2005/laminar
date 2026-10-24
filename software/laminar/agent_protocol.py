"""Messages between laminar-lab (the public entry agent on Agentverse / ASI:One) and the planner + orchestrator
agent behind it (Om's: Claude turns a request into a plan, the executor runs it).

    entry agent                              planner + orchestrator agent
    BenchQuery              ------------->   BenchView      what is on the bench now
    PlanRequest             ------------->   PlanProposal   request -> plan (or an error in words)
    RunRequest (approved)   ------------->   RunReport      executor report after the run

The entry agent keeps the chat, checks every proposed plan with lab_agent.validate_plan and asks a human to
approve before it sends a RunRequest. A plan is the executor's plan.json shape:
    {"plan_id": "...", "request": "...", "steps": [{"skill": "pick", "args": {...}}, ...]}
A bench view is lab_agent.bench_view()'s shape; a run report is executor.RunReport.to_dict().

To build the planner side: include `planner_protocol` in your uAgent and answer each request with its reply
type, e.g. `await ctx.send(sender, PlanProposal(...))` (planner_agent.py is a working reference).
"""
from __future__ import annotations

from uagents import Model, Protocol


class BenchQuery(Model):
    requester: str = ""


class BenchView(Model):
    view: dict


class PlanRequest(Model):
    request_id: str
    text: str                 # the user's words, as typed in ASI:One
    bench: dict               # the entry agent's bench view, for context
    requester: str            # who asked (ASI:One user address)


class PlanProposal(Model):
    request_id: str
    plan: dict | None = None  # None when the request can't be planned
    error: str = ""           # why not, in words for the user (or a clarifying question)


class RunRequest(Model):
    plan: dict
    approved_by: str


class RunReport(Model):
    report: dict


planner_protocol = Protocol(name="LaminarPlanner", version="0.1.0")
