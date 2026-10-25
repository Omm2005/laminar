# Laminar on Fetch.ai: other labs ask our robot to run tests

**Intent to action:** a lab writes "pour 1 mL of the blue dye into container 2 and tell me what the camera measures"
in ASI:One, or sends it from its own agent. A real robot arm in our lab plans the job and checks it against the
bench. A technician approves it, the arm pours with closed-loop camera control, and the measured volumes come back
into the same conversation.

```
ASI:One user  ─┐                                   ┌─► Lab assistant (Claude + tools: get_bench_state, submit_plan,
partner lab    ├─ Agent Chat Protocol ─► Laminar ──┤   request_approval, get_run_report) checks vials, volumes,
QC agent      ─┘   (Agentverse mailbox)   Bench    │   overflow and reach
                                          agent    ├─► permission card on the bench dashboard ─► technician approves
                ◄── plan, approval, "running", ────┤
                    measured mL per container      └─► arm + camera run it (console/bridge/run_plan.py)
```

| Part | File | What it does |
| --- | --- | --- |
| Laminar Bench agent | `fetch/bench_agent.py` | uAgent with the **Agent Chat Protocol**, registered on **Agentverse** (mailbox), found by **ASI:One** through `AGENT_README.md`. It relays each request into its own chat thread on the bench, then sends the replies, plan, approval, start and results back. |
| Partner lab QC agent | `fetch/partner_lab_agent.py` | **Agent to agent:** another lab's agent orders a dispensing QC series. It approves its own plan on the simulated bench, checks each measured volume against its spec, orders **one top-up** for containers that came out under, and writes `fetch/reports/qc_*.json`. |
| Bench backend | `console/convex/remote.ts` | Gives each remote lab its own thread with the same assistant, tools and checks as the Lab page. Requesters can only see and cancel their own plans. They can approve only on a **simulated** bench. |
| Lab page | `console/src/pages/Lab.tsx` | "Requests from other labs": permission cards labelled with the lab's name. The technician approves or rejects them there. |
| Robot | `console/bridge/run_plan.py`, `software/laminar` | Runs approved plans on the real arm (`start.sh`) or the simulated one (`start.sh sim`). |

Safety is built into the code, not the prompt. The agent can only propose: the backend has no approve path for
remote requesters on the real arm. Every plan is checked against what is loaded, with at most 5 mL per pour, at least
0.5 mL kept in the vial and no container filled past 97 % of its rim. Every request, approval, step and camera reading
goes into the Audit page.

## Run it

Needs Python 3.11+, [bun](https://bun.sh) and, optionally, an Anthropic API key (without it, a rule parser stands
in for Claude).

```bash
git clone https://github.com/Omm2005/laminar.git && cd laminar && git checkout fetch-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r sense/requirements.txt convex pyyaml
python3 -m venv fetch/.venv && fetch/.venv/bin/pip install -r fetch/requirements.txt

# secrets live in .env (git-ignored)
cat >> .env <<EOF
ANTHROPIC_API_KEY=sk-ant-...
BENCH_AGENT_SEED=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
PARTNER_AGENT_SEED=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
EOF

# terminal 1: the bench (dashboard backend + website + bench link). "sim" = simulated arm and camera
console/start.sh sim                      # http://localhost:5173 ; on the robot computer: console/start.sh

# terminal 2: the Laminar Bench agent
fetch/.venv/bin/python fetch/bench_agent.py
```

On the dashboard, add the vials on the **Bench** page (for example vial A "Red dye" 4 mL and vial B "Blue dye" 4 mL).
The containers come from the camera, or from the simulated bench.

**Put it on Agentverse (once).** Open the *Agent inspector* link that `bench_agent.py` prints, click **Connect**,
then choose **Mailbox**. If the browser can't reach the agent on this computer, create an API key on agentverse.ai
instead (Profile > API Keys) and put `AGENTVERSE_API_KEY=...` in `.env`. Then run
`fetch/.venv/bin/python fetch/connect_mailbox.py` and restart the agent. It now appears under *My Agents*, with
`AGENT_README.md` as its profile, and ASI:One can find it.

## Test it

**In ASI:One** (asi1.ai): ask *"Find the Laminar Bench agent: what's on its bench?"*, or open the agent on
Agentverse and click *Chat with Agent*. Then try:

1. `Pour 1 mL of the blue dye into container 2 and tell me what the camera measures.` You get the checked plan.
2. `approve` on the simulated bench. On the real arm, the technician approves on the Lab page instead.
3. You get "The robot is running plan …", then the result: measured mL, error against the target, within
   tolerance or not.
4. `status` lists your runs and results. `cancel` withdraws a waiting plan.

How it talks to ASI:One: one reply per message, ending the turn (as ASI:One expects). The only unprompted message
is the run's result. It doesn't answer pure acknowledgements ("Understood", "Noted"), and it answers at most 4
messages per 2 minutes to one sender, so it can't get into a back-and-forth with ASI:One's own assistant. Short plan
ids from the chat ("plan 8gyfhw") work everywhere.

Things it should refuse, with the reason: more than is left in the vial, a pour that would overflow a container, a
liquid that isn't loaded.

**Agent to agent** (both agents on one machine, no Agentverse needed):

```bash
BENCH_AGENT_MAILBOX=0 fetch/.venv/bin/python fetch/bench_agent.py      # prints its address
fetch/.venv/bin/python fetch/partner_lab_agent.py --bench <that address> --series 0.5:1,1.0:3,1.5:4
```

The partner agent orders the series and approves it on the simulated bench. It reads the results, orders a top-up
if any container is under spec, and writes `fetch/reports/qc_<time>.json` with PASS or FAIL per container.

`software/laminar/fetch_agent.py` is an earlier standalone ASI:One agent. It runs the executor inside the agent
process, and runs are approved by the ASI:One users listed in `LAMINAR_APPROVERS`. The `fetch/` agents here go
through the bench dashboard instead, so the technician's permission card and the audit trail apply to other labs'
requests too.
