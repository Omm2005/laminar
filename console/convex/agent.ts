"use node";

import { internalAction } from "./_generated/server";
import { v } from "convex/values";
import { internal, api } from "./_generated/api";
import Anthropic from "@anthropic-ai/sdk";

/** Lab chat agent. Claude reads the request and may call three tools:
 *    get_bench_state  - vials and containers, by name, with volumes
 *    propose_plan     - pours [{vial, container, ml}]. CODE checks them and builds the robot steps; the plan is
 *                       shown in the chat as a card that a PERSON approves or rejects (button, voice or typing)
 *    get_run_report   - what happened in a run, for "why did it stop?"
 *  There is no approve tool: the agent can propose, never start the robot.
 *  Without ANTHROPIC_API_KEY a small rule parser stands in, so the dashboard works offline.
 *
 *  Names (vial "Blue dye", container "Sample 2") live only here and in the dashboard; the robot gets ids
 *  (vial_A, well_2). Picking and placing are placeholders until the grab is built.
 */

const MODEL = "claude-opus-5-5";
const MAX_ML = 5;              // one pour never exceeds this
const KEEP_IN_VIAL_ML = 0.5;
const MIN_FILL = 0.1;          // the camera pour aims at >= 10 % of a container
const MAX_FILL = 0.97;         // ... and never past 97 % of the rim

type Vial = { slot: number; letter: string; contents: string; volumeMl: number };
type Channel = { number: number; contents: string; currentMl: number; capacityMl: number };
type Pour = { vial: number; container: number; ml: number };
type PlanStep = { name: string; args: Record<string, unknown>; label: string; placeholder?: boolean };

/* ------------------------------------------------------------------ bench + plan (code, not the model) */

function round(x: number) { return Math.round(x * 100) / 100; }

type Station = { robot: string | null; grabs: string[]; reachable: number[] };

function benchState(vials: Vial[], channels: Channel[], tolerance: number, st: Station) {
  return {
    vials: vials.map((v) => ({ vial: v.letter, name: v.contents, ml: round(v.volumeMl),
                               grab: st.grabs.includes(`vial_${v.letter}`) ? "recorded" : "placeholder (operator loads it)" })),
    containers: channels.map((c) => ({
      container: c.number, name: c.contents, ml_seen_by_camera: round(c.currentMl), capacity_ml: c.capacityMl,
      arm_can_reach: st.reachable.includes(c.number),
    })),
    tolerance_ml: tolerance,
    robot_ready: st.robot === null,
    robot_not_ready_because: st.robot,
    operations: "pick (recorded grab), carry to a container or back to the rack, pour (camera-controlled), lift / " +
                "lower N mm, place (recorded grab backwards). A full pour = pick, carry, pour, check, carry back, place.",
  };
}

/** One robot operation in dashboard terms. A plan is a list of these; "pours" are shorthand for full transfers. */
type Op =
  | { op: "pick"; vial: number }
  | { op: "carry"; to: number | "rack" }
  | { op: "pour"; container: number; ml: number }
  | { op: "lift" | "lower"; mm: number }
  | { op: "place"; vial: number };

function poursToOps(pours: Pour[]): Op[] {
  return pours.flatMap((p): Op[] => [
    { op: "pick", vial: p.vial }, { op: "carry", to: p.container }, { op: "pour", container: p.container, ml: p.ml },
    { op: "carry", to: "rack" }, { op: "place", vial: p.vial },
  ]);
}

/** Check the operations in order against the bench and build the robot's steps (ids only), or the problems. */
function buildPlan(ops: Op[], vials: Vial[], channels: Channel[], tolerance: number, st: Station):
    { steps: PlanStep[]; pours: Pour[]; problems: string[] } {
  const problems: string[] = [];
  const steps: PlanStep[] = [];
  const pours: Pour[] = [];
  if (!ops.length) problems.push("the plan has no steps");
  let held: Vial | null = null;
  let at: "slot" | number | "moved" | null = null;
  const added = new Map<number, number>();
  const taken = new Map<number, number>();
  ops.forEach((o, i) => {
    const where = ops.length > 1 ? `step ${i + 1} (${o.op}): ` : "";
    const bad = (msg: string) => problems.push(where + msg);
    if (o.op === "pick") {
      const v = vials.find((x) => x.slot === o.vial);
      if (!v) return bad(vials.length ? `there is no vial ${String.fromCharCode(64 + o.vial)}` : "no vials are set up yet (Bench page)");
      if (held) return bad(`already holding vial ${held.letter}`);
      held = v; at = "slot";
      const recorded = st.grabs.includes(`vial_${v.letter}`);
      steps.push({ name: "pick", args: { vial_id: `vial_${v.letter}` }, label: `Pick up vial ${v.letter} (${v.contents})`,
                   ...(recorded ? {} : { placeholder: true }) });
    } else if (o.op === "carry") {
      if (!held) return bad("nothing in the gripper to carry");
      const h: Vial = held;
      if (o.to === "rack") {
        if (typeof at !== "number") return bad("carry back only works from a container");
        at = "slot";
        steps.push({ name: "carry", args: { to: "rack" }, label: `Carry vial ${h.letter} back` });
      } else {
        const ch = channels.find((c) => c.number === o.to);
        if (!ch) return bad(`there is no container ${o.to}` + (channels.length ? "" : " (calibrate the side camera)"));
        if (at !== "slot") return bad("a carry to a container starts from the vial's slot (pick it up first)");
        if (!st.reachable.includes(ch.number)) bad(`the arm has no taught pour spot over ${ch.contents} yet`);
        at = ch.number;
        steps.push({ name: "carry", args: { to: `well_${ch.number}` }, label: `Carry it to ${ch.contents}` });
      }
    } else if (o.op === "pour") {
      const ch = channels.find((c) => c.number === o.container);
      if (!ch) return bad(`there is no container ${o.container}`);
      if (!held) return bad("nothing in the gripper to pour");
      const h: Vial = held;
      if (at !== ch.number) return bad(`carry the vial to ${ch.contents} before pouring`);
      if (!(o.ml > 0 && o.ml <= MAX_ML)) return bad(`${o.ml} mL: one pour must be more than 0 and at most ${MAX_ML} mL`);
      const after = ch.currentMl + (added.get(ch.number) ?? 0) + o.ml;
      if (after > MAX_FILL * ch.capacityMl) bad(`${ch.contents} holds ${round(ch.currentMl)} of ${ch.capacityMl} mL; ${o.ml} mL more would overflow`);
      if (after < MIN_FILL * ch.capacityMl) bad(`${ch.contents} would hold only ${round(after)} mL; the camera pour needs at least ${round(MIN_FILL * ch.capacityMl)} mL in it at the end`);
      const fromVial = (taken.get(h.slot) ?? 0) + o.ml;
      if (fromVial > h.volumeMl - KEEP_IN_VIAL_ML) bad(`vial ${h.letter} has ${round(h.volumeMl)} mL; ${KEEP_IN_VIAL_ML} mL must stay in it`);
      added.set(ch.number, (added.get(ch.number) ?? 0) + o.ml);
      taken.set(h.slot, fromVial);
      pours.push({ vial: h.slot, container: ch.number, ml: round(o.ml) });
      const well = `well_${ch.number}`;
      steps.push({ name: "pour", args: { target: well, ml: round(o.ml), liquid: null }, label: `Pour ${round(o.ml)} mL into ${ch.contents}` },
                 { name: "verify", args: { target: well, expect_ml: round(o.ml), tol: tolerance },
                   label: `Check ${ch.contents} gained ${round(o.ml)} ± ${tolerance} mL` });
    } else if (o.op === "lift" || o.op === "lower") {
      if (!(o.mm >= 1 && o.mm <= 150)) return bad(`${o.mm} mm: move between 1 and 150 mm`);
      at = "moved";
      steps.push({ name: o.op, args: { mm: round(o.mm) }, label: `${o.op === "lift" ? "Lift" : "Lower"} ${round(o.mm)} mm` });
    } else if (o.op === "place") {
      const v = vials.find((x) => x.slot === o.vial);
      if (!v) return bad(`there is no vial ${String.fromCharCode(64 + o.vial)}`);
      if (held?.slot !== v.slot) return bad(`not holding vial ${v.letter}`);
      if (at !== "slot") return bad(`carry vial ${v.letter} back before putting it down`);
      held = null; at = null;
      const recorded = st.grabs.includes(`vial_${v.letter}`);
      steps.push({ name: "place", args: { vial_id: `vial_${v.letter}`, slot: `slot_${v.letter}` }, label: `Put vial ${v.letter} back`,
                   ...(recorded ? {} : { placeholder: true }) });
    } else {
      bad(`unknown operation`);
    }
  });
  return { steps, pours, problems };
}

function describePour(p: Pour, vials: Vial[], channels: Channel[]) {
  const vial = vials.find((v) => v.slot === p.vial)!;
  const ch = channels.find((c) => c.number === p.container)!;
  return { ...p, vialLetter: vial.letter, vialName: vial.contents, containerName: ch.contents,
           containerMlNow: round(ch.currentMl), capacityMl: ch.capacityMl };
}

/* ------------------------------------------------------------------ tools (the PRD's four) */

const OP_SCHEMA = {
  type: "object",
  properties: {
    op: { type: "string", enum: ["pick", "carry", "pour", "lift", "lower", "place"] },
    vial: { type: "string", description: "pick / place: vial letter" },
    to: { type: "string", description: "carry: a container number, or \"rack\" to carry the vial back" },
    container: { type: "integer", description: "pour: container number" },
    ml: { type: "number", description: "pour: mL to add" },
    mm: { type: "number", description: "lift / lower: millimetres, 1 to 150" },
  },
  required: ["op"], additionalProperties: false,
};

const TOOLS: Anthropic.Beta.BetaTool[] = [
  {
    name: "get_bench_state",
    description: "Vials (letter, name, mL, whether its grab is recorded), containers (number, name, mL the camera " +
      "sees, capacity, whether the arm can reach it), the pour tolerance, and whether the robot is ready.",
    input_schema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "submit_plan",
    description: "Submit a plan; code checks it against the bench and returns the robot steps or the problems. " +
      "Give EITHER pours (each = a full transfer: pick, carry, pour, check, carry back, place) OR steps (single " +
      "operations, for requests like \"lift the arm 50 mm\" or \"carry vial A to container 2\"). A submitted plan " +
      "is not shown to the technician until you call request_approval.",
    input_schema: {
      type: "object",
      properties: {
        pours: {
          type: "array", minItems: 1,
          items: {
            type: "object",
            properties: {
              vial: { type: "string", description: "vial letter, e.g. \"A\"" },
              container: { type: "integer", description: "container number" },
              ml: { type: "number", description: "mL to add to that container" },
            },
            required: ["vial", "container", "ml"], additionalProperties: false,
          },
        },
        steps: { type: "array", minItems: 1, items: OP_SCHEMA },
      },
      additionalProperties: false,
    },
  },
  {
    name: "request_approval",
    description: "Show a submitted plan to the technician as a permission card (Approve / Reject). Only a person can " +
      "approve; the robot does nothing until they do.",
    input_schema: { type: "object", properties: { plan_id: { type: "string" } }, required: ["plan_id"], additionalProperties: false },
  },
  {
    name: "get_run_report",
    description: "What happened in the latest run (or a given plan id): each step's result, measured volumes, why it " +
      "stopped. Use it for questions like \"why did it stop?\".",
    input_schema: { type: "object", properties: { plan_id: { type: "string" } }, additionalProperties: false },
  },
];

const SYSTEM = `You are Laminar, the assistant for a lab robot arm that pours measured volumes of liquid from vials
into containers and checks the result with a camera. You talk with a lab technician by chat or voice.

The loop: read the request -> get_bench_state if unsure -> submit_plan (code checks it) -> fix and resubmit if it
reports problems -> request_approval (a person approves on screen) -> after the run, get_run_report to explain.
You never start the robot yourself and you never say a plan is approved or running.

- Refer to vials and containers by name as well as letter/number ("vial A, Red sample").
- Pour requests become pours ("1 mL each into containers 1 and 2" = one plan, two pours). Single moves ("lift 50 mm",
  "carry vial A to container 2", "pour 1 mL here") become steps.
- If the vial, the container or the volume is unclear, ask one short question. Never invent a volume.
- Steps whose grab is not recorded are placeholders: an operator loads the vial; say so when it matters.
- If the robot is not ready, say why plainly; you may still prepare a plan for review.
- Replies are read aloud: one to three short sentences, no markdown.`;

/** Added for a request from another lab's agent (remote.ts: Fetch.ai uAgents, ASI:One). */
function remoteNote(label: string) {
  return `

This conversation is NOT with the technician: it is with ${label}, another lab, writing through its agent (Fetch.ai
ASI:One). They can't see this bench. Same loop: plan, check, request_approval. After request_approval tell them the plan
is waiting for the technician at this bench (on a simulated bench they may reply "approve" themselves), and that you will
message them when it runs and with the measured volumes. Give container numbers and vial letters with names. Your reply
is shown as chat text: two to four short sentences, plain text.`;
}

/* ------------------------------------------------------------------ chat */

type RawPour = { vial: string | number; container: number; ml: number };
type RawOp = { op: string; vial?: string; to?: string; container?: number; ml?: number; mm?: number };

function letterSlot(v: string | number | undefined): number {
  return typeof v === "number" ? v : String(v ?? "").trim().toUpperCase().charCodeAt(0) - 64;
}

function toOps(raw: RawOp[]): Op[] {
  return raw.map((o): Op => {
    switch (o.op) {
      case "pick": return { op: "pick", vial: letterSlot(o.vial) };
      case "place": return { op: "place", vial: letterSlot(o.vial) };
      case "carry": return { op: "carry", to: String(o.to).toLowerCase().includes("rack") || String(o.to).toLowerCase() === "back"
                                               ? "rack" : Number(String(o.to).replace(/\D/g, "")) };
      case "pour": return { op: "pour", container: Number(o.container), ml: Number(o.ml) };
      case "lift": case "lower": return { op: o.op, mm: Number(o.mm) };
      default: return { op: "lift", mm: NaN };
    }
  });
}

export const chat = internalAction({
  args: { threadId: v.string(), userMessage: v.string() },
  handler: async (ctx, args) => {
    const load = async () => {
      const [vials, channels, tol, robot, station] = await Promise.all([
        ctx.runQuery(api.vials.list, {}) as Promise<Vial[]>,
        ctx.runQuery(api.channels.list, {}) as Promise<Channel[]>,
        ctx.runQuery(api.settings.get, { key: "tolerance_ml" }) as Promise<number | null>,
        ctx.runQuery(internal.station.readyInternal, {}) as Promise<string | null>,
        ctx.runQuery(internal.station.capabilitiesInternal, {}),
      ]);
      const st: Station = { robot, grabs: station?.grabs ?? [], reachable: station?.reachable ?? [] };
      return { vials, channels, tolerance: Number(tol ?? 0.2), st };
    };
    const say = (text: string, tool?: { name: string; args: unknown }) =>
      ctx.runMutation(internal.messages.appendInternal, { threadId: args.threadId, role: "assistant", text, tool });
    const cards: { text: string; tool: { name: string; args: unknown } }[] = [];

    /** submit_plan: code checks the operations and stores the plan (not shown yet). */
    const submit = async (input: { pours?: RawPour[]; steps?: RawOp[] }) => {
      const { vials, channels, tolerance, st } = await load();
      const ops = input.steps?.length ? toOps(input.steps)
        : poursToOps((input.pours ?? []).map((p) => ({ vial: letterSlot(p.vial), container: Number(p.container), ml: round(Number(p.ml)) })));
      const { steps, pours, problems } = buildPlan(ops, vials, channels, tolerance, st);
      if (problems.length) return { ok: false as const, problems };
      const jobId = await ctx.runMutation(internal.jobs.createPlanInternal, {
        threadId: args.threadId, requestText: args.userMessage, pours, plan: steps,
      });
      return { ok: true as const, plan_id: jobId, steps: steps.map((s) => s.label + (s.placeholder ? " (placeholder)" : "")),
               robot_ready: st.robot === null, robot_not_ready_because: st.robot };
    };

    /** A plan by its id, or by the short id shown in chats (its last characters, e.g. "8gyfhw"). */
    const findPlan = async (planId: string) => {
      const id = planId.trim();
      if (!id) return null;
      const recent = await ctx.runQuery(api.jobs.list, { limit: 100 });
      return recent.find((j) => j._id === id) ?? recent.find((j) => j.threadId === args.threadId && j._id.endsWith(id)) ?? null;
    };

    /** request_approval: put the plan in front of a person. */
    const requestApproval = async (planId: string) => {
      const { vials, channels } = await load();
      const job = await findPlan(planId);
      if (!job || job.status !== "queued") return { ok: false as const, problems: ["no submitted plan with that id"] };
      await ctx.runMutation(internal.jobs.requestApprovalInternal, { jobId: job._id });
      cards.push({
        text: "Permission requested",
        tool: { name: "plan", args: { jobId: job._id,
                                      pours: (job.pours ?? []).map((p) => describePour(p, vials, channels)),
                                      steps: job.plan ?? [] } },
      });
      return { ok: true as const, shown: true, note: "The technician sees Approve / Reject. It is NOT approved." };
    };

    const report = async (planId?: string) => {
      const job = planId
        ? await findPlan(planId)
        : (await ctx.runQuery(api.jobs.list, { limit: 10 })).find((j) =>
            !["awaiting_approval", "rejected", "planning", "queued"].includes(j.status));
      if (!job) return { found: false as const };
      type S = { skill: string; status: string; reason?: string; telemetry?: unknown };
      return { found: true as const, plan_id: job._id, request: job.requestText, status: job.status,
               fail_reason: job.failReason ?? null, approved_by: job.approvedBy ?? null,
               steps: ((job.report?.steps ?? []) as S[]).map((s) =>
                 ({ skill: s.skill, status: s.status, reason: s.reason, telemetry: s.telemetry })),
               results: job.report?.results ?? null, abort: job.report?.abort ?? null };
    };

    const remote = args.threadId.startsWith("remote:")
      ? await ctx.runQuery(internal.remote.getInternal, { threadId: args.threadId }) : null;
    const system = remote ? SYSTEM + remoteNote(remote.label) : SYSTEM;
    const key = process.env.ANTHROPIC_API_KEY;
    try {
      if (!key) {
        const { vials, channels, tolerance, st } = await load();
        const r = ruleReply(args.userMessage, vials, channels, tolerance, st.robot);
        if (r.kind === "plan") {
          const res = await submit(r.ops.length ? { steps: r.ops as unknown as RawOp[] } : { pours: r.pours });
          if (!res.ok) await say("I can't plan that: " + res.problems.join("; ") + ".");
          else {
            await requestApproval(res.plan_id);
            await say(st.robot ? `Here's the plan to review. It can't run yet: ${st.robot}.`
              : remote ? "The plan is checked and waiting for the technician at the bench to approve. I'll message you when it runs."
              : "Here's the plan. Approve or reject it.");
          }
        } else if (r.kind === "report") {
          const rep = await report();
          await say(!rep.found ? "No runs yet." :
            `The last run ("${rep.request}") ended ${rep.status}${rep.fail_reason ? ` because ${rep.fail_reason}` : ""}.`);
        } else {
          await say(r.text);
        }
      } else {
        const client = new Anthropic({ apiKey: key });
        const history = (await ctx.runQuery(internal.messages.listInternal,
                                            { threadId: args.threadId, limit: 30, since: remote?.sessionStart }))
          .filter((m) => m.role !== "tool" && m.text)
          .map((m) => ({ role: m.role === "user" ? ("user" as const) : ("assistant" as const), content: m.text }));
        const messages: Anthropic.Beta.BetaMessageParam[] = mergeRoles(history);
        let final = "";
        for (let turn = 0; turn < 8; turn++) {
          const resp = await client.beta.messages.create({
            model: MODEL, max_tokens: 4096, system, tools: TOOLS, messages,
            output_config: { effort: "low" },
            betas: ["server-side-fallback-2026-07-01"], fallbacks: "default",
          });
          if (resp.stop_reason === "refusal") { final = "I can't help with that request."; break; }
          messages.push({ role: "assistant", content: resp.content });   // thinking blocks passed back unchanged
          const uses = resp.content.filter((b): b is Anthropic.Beta.BetaToolUseBlock => b.type === "tool_use");
          final = resp.content.filter((b): b is Anthropic.Beta.BetaTextBlock => b.type === "text").map((b) => b.text).join("");
          if (resp.stop_reason !== "tool_use" || !uses.length) break;
          const results: Anthropic.Beta.BetaToolResultBlockParam[] = [];
          for (const u of uses) {
            let out: unknown;
            const input = u.input as Record<string, unknown>;
            if (u.name === "get_bench_state") {
              const { vials, channels, tolerance, st } = await load();
              out = benchState(vials, channels, tolerance, st);
            } else if (u.name === "submit_plan") {
              out = await submit(input as { pours?: RawPour[]; steps?: RawOp[] });
            } else if (u.name === "request_approval") {
              out = await requestApproval(String(input.plan_id ?? ""));
            } else if (u.name === "get_run_report") {
              out = await report(input.plan_id as string | undefined);
            } else {
              out = { error: `unknown tool ${u.name}` };
            }
            const failed = !!out && typeof out === "object" && "ok" in out && (out as { ok: boolean }).ok === false;
            results.push({ type: "tool_result", tool_use_id: u.id, content: JSON.stringify(out), is_error: failed });
          }
          messages.push({ role: "user", content: results });
        }
        await say(final || (cards.length ? "Here's the plan. Approve or reject it." : "(no reply)"));
      }
    } catch (e) {
      await say(`Sorry, the assistant hit an error: ${(e as Error).message}`);
    }
    for (const c of cards) await say(c.text, c.tool);
    return null;
  },
});

function mergeRoles(rows: { role: "user" | "assistant"; content: string }[]): Anthropic.Beta.BetaMessageParam[] {
  const out: { role: "user" | "assistant"; content: string }[] = [];
  for (const r of rows) {
    const last = out[out.length - 1];
    if (last && last.role === r.role) last.content += "\n" + r.content;
    else out.push({ ...r });
  }
  while (out.length && out[0].role !== "user") out.shift();
  return out;
}

/* ------------------------------------------------------------------ after a run: explain it */

type ReportStep = { skill: string; status: string; reason?: string; telemetry?: Record<string, unknown> };

export const explain = internalAction({
  args: { jobId: v.id("jobs") },
  handler: async (ctx, args) => {
    const job = await ctx.runQuery(api.jobs.get, { jobId: args.jobId });
    if (!job?.threadId) return null;
    const steps = ((job.report?.steps ?? []) as ReportStep[]).map((s) => ({
      skill: s.skill, status: s.status, reason: s.reason || undefined,
      measured_ml: s.telemetry?.measured_ml, error_ml: s.telemetry?.error_ml, placeholder: s.telemetry?.placeholder,
    }));
    const facts = { request: job.requestText, status: job.status, fail_reason: job.failReason ?? null, steps,
                    results: job.report?.results ?? null, abort: job.report?.abort ?? null };
    let text = templateSummary(job.status, job.failReason ?? null, steps);
    const key = process.env.ANTHROPIC_API_KEY;
    if (key) {
      try {
        const client = new Anthropic({ apiKey: key });
        const resp = await client.beta.messages.create({
          model: MODEL, max_tokens: 1024, output_config: { effort: "low" },
          betas: ["server-side-fallback-2026-07-01"], fallbacks: "default",
          system: "You explain a lab robot run to the technician in two or three plain sentences: what was done, the " +
                  "measured volume against the target, and if it stopped, why and what to do next. Steps marked " +
                  "placeholder were done by hand. No markdown.",
          messages: [{ role: "user", content: JSON.stringify(facts) }],
        });
        const t = resp.content.filter((b): b is Anthropic.Beta.BetaTextBlock => b.type === "text").map((b) => b.text).join("");
        if (resp.stop_reason !== "refusal" && t.trim()) text = t.trim();
      } catch { /* keep the template */ }
    }
    await ctx.runMutation(internal.jobs.setSummaryInternal, { jobId: args.jobId, summary: text });
    await ctx.runMutation(internal.messages.appendInternal, {
      threadId: job.threadId, role: "assistant", text,
      tool: { name: "run_summary", args: { jobId: args.jobId, status: job.status } },
    });
    return null;
  },
});

function templateSummary(status: string, failReason: string | null,
                         steps: { skill: string; status: string; reason?: string; measured_ml?: unknown; error_ml?: unknown }[]) {
  const measured = steps.filter((s) => s.skill === "verify" && s.measured_ml != null)
    .map((s) => `${Number(s.measured_ml).toFixed(2)} mL (off by ${Number(s.error_ml).toFixed(2)})`);
  if (status === "done") return `Done. The camera measured ${measured.join(", ") || "the pour"}, within tolerance.`;
  const failed = steps.find((s) => s.status === "failed");
  const why = failed?.reason || failReason || "an unknown reason";
  return `Stopped at the ${failed ? failed.skill : "current"} step: ${why}. The wrist is upright and the arm is holding where it is.`;
}

/* ------------------------------------------------------------------ the offline rule parser */

type Rule = { kind: "plan"; pours: Pour[]; ops: Op[] } | { kind: "report" } | { kind: "text"; text: string };

function ruleReply(text: string, vials: Vial[], channels: Channel[], _tolerance: number, robot: string | null): Rule {
  const t = text.toLowerCase().trim();
  const containers = channels;
  const help = `Try "pour 1 mL of ${vials[0]?.contents ?? "vial A"} into ${containers[0]?.contents ?? "container 1"}", ` +
               `or "what's on the bench?".`;
  if (/\b(bench|vials|containers|inventory)\b|what('s| is) (on|there)/.test(t)) {
    const vs = vials.length ? vials.map((x) => `${x.letter} ${x.contents} (${round(x.volumeMl)} mL)`).join(", ")
                            : "none set up yet (add them on the Bench page)";
    const cs = containers.length ? containers.map((c) => `${c.number} ${c.contents} (${round(c.currentMl)} of ${c.capacityMl} mL)`).join(", ")
                                 : "none yet (calibrate the side camera)";
    return { kind: "text", text: `Vials: ${vs}. Containers: ${cs}.` + (robot ? ` Robot: ${robot}.` : " Robot: ready.") };
  }
  if (/\b(why|what happened|stopped|failed|report)\b/.test(t) && !/\bpour\b/.test(t)) return { kind: "report" };
  const move = t.match(/\b(lift|raise|move up|up|lower|move down|down)\b[^\d]*(\d+(?:\.\d+)?)\s*mm\b/);
  if (move) {
    const up = /lift|raise|up/.test(move[1]);
    return { kind: "plan", pours: [], ops: [{ op: up ? "lift" : "lower", mm: Number(move[2]) }] };
  }
  if (/\d/.test(t) && /\b(ml|mls|milliliters?|millilitres?)\b/.test(t)) {
    if (!vials.length) return { kind: "text", text: "No vials are set up yet: add them on the Bench page first." };
    if (!containers.length) return { kind: "text", text: "No containers yet: calibrate the side camera on the Lab page first." };
  }
  const pours: Pour[] = [];
  for (const part of t.split(/\band then\b|\bthen\b|\band\b|;/)) {
    const m = part.match(/(\d+(?:\.\d+)?)\s*(?:ml|mls|milliliters?|millilitres?)\b/);
    if (!m) continue;
    const vial = findVial(part, vials) ?? (pours.length ? pours[pours.length - 1].vial : findVial(t, vials));
    const container = findContainer(part, containers);
    if (vial == null) return { kind: "text", text: "Which vial? " + vials.map((x) => `${x.letter} (${x.contents})`).join(", ") + "." };
    if (container == null) return { kind: "text", text: "Into which container? " +
      containers.map((c) => `${c.number} (${c.contents})`).join(", ") + "." };
    pours.push({ vial, container, ml: Number(m[1]) });
  }
  if (pours.length) return { kind: "plan", pours, ops: [] };
  if (/\d/.test(t) || /\b(pour|fill|add|put)\b/.test(t)) return { kind: "text", text: "How many mL, from which vial, into which container? " + help };
  return { kind: "text", text: "I pour measured volumes and explain runs. " + help };
}

function findVial(t: string, vials: Vial[]): number | null {
  const m = t.match(/\bvial\s+([a-d])\b/);
  if (m) return m[1].toUpperCase().charCodeAt(0) - 64;
  const words = (v: Vial) => v.contents.toLowerCase().split(/[\s()]+/).filter((w) => w.length > 2 && !["dye", "the"].includes(w));
  const hit = vials.find((v) => words(v).some((w) => new RegExp(`\\b${w}\\b`).test(t)));
  return hit ? hit.slot : null;
}

function findContainer(t: string, containers: Channel[]): number | null {
  const named = containers.find((c) => t.includes(c.contents.toLowerCase()));
  if (named) return named.number;
  const m = t.match(/\b(?:container|well|compartment|channel|tube)\s*#?\s*(\d+)\b/);
  if (m) return Number(m[1]);
  const col = t.match(/\bcolumn\s+([a-d])\b/);
  return col ? col[1].toUpperCase().charCodeAt(0) - 64 : null;
}

/* ------------------------------------------------------------------ bench setup from the calibration picture */

const SETUP_SCHEMA = {
  type: "object",
  properties: {
    containers: {
      type: "array",
      items: {
        type: "object",
        properties: {
          number: { type: "integer", description: "the box number drawn on the picture" },
          name: { type: "string", description: "short name a technician would use, max 24 characters, e.g. \"Red sample\" or \"Empty\"" },
          contents: { type: "string", enum: ["empty", "liquid", "unclear"] },
          liquid_color: { type: ["string", "null"], description: "colour of the liquid, null if empty or unclear" },
          box_fits: { type: "boolean", description: "the box covers exactly one container, top on the rim, bottom on the floor, inside the walls" },
          problem: { type: ["string", "null"], description: "what is wrong with this box, in a few words; null if it fits" },
        },
        required: ["number", "name", "contents", "liquid_color", "box_fits", "problem"],
        additionalProperties: false,
      },
    },
    missed: { type: "integer", description: "containers visible in the picture that have no box" },
    summary: { type: "string", description: "one or two plain sentences for the technician" },
  },
  required: ["containers", "missed", "summary"],
  additionalProperties: false,
} as const;

type SetupResult = {
  containers: { number: number; name: string; contents: string; liquid_color: string | null; box_fits: boolean;
                problem: string | null }[];
  missed: number;
  summary: string;
};

/** After a calibration: Claude looks at the camera picture with the numbered boxes, names each container from what
 *  it sees, checks every box sits on its container, and cross-checks the level model. Results go to the chat. */
export const setupBench = internalAction({
  args: {},
  handler: async (ctx) => {
    const say = (text: string, tool?: { name: string; args: unknown }) =>
      ctx.runMutation(internal.messages.appendInternal, { threadId: "main", role: "assistant", text, tool });
    const cal = await ctx.runQuery(internal.station.calibInternal, {});
    if (!cal?.calibFrameId || !cal.boxes.length) return null;
    const n = cal.boxes.length;
    const key = process.env.ANTHROPIC_API_KEY;
    if (!key) {
      await say(`Calibration saved: ${n} container${n === 1 ? "" : "s"}. Add a Claude API key to have Claude name ` +
                "them from the camera picture and check the boxes.");
      return null;
    }
    const blob = await ctx.storage.get(cal.calibFrameId);
    if (!blob) return null;
    const image = Buffer.from(await blob.arrayBuffer()).toString("base64");
    const readings = cal.containers.map((c) => ({ number: c.number, model_ml: c.ml, model_confidence: c.conf,
                                                  capacity_ml: c.capacityMl }));
    try {
      const client = new Anthropic({ apiKey: key });
      const resp = await client.beta.messages.create({
        model: MODEL, max_tokens: 4096,
        output_config: { effort: "medium", format: { type: "json_schema", schema: SETUP_SCHEMA } },
        betas: ["server-side-fallback-2026-07-01"], fallbacks: "default",
        system: "You set up a lab robot's bench from its side camera. The picture shows the containers the robot " +
                "pours into, seen from the side, with one numbered magenta box per container (numbered left to right). " +
                "A level model reads the liquid height inside each box. For each box: give the container a short name " +
                "from what you see (its liquid colour, or 'Empty'), say whether it holds liquid, and check the box: " +
                "top edge on the container's rim, bottom edge on its floor, sides just inside its walls, exactly one " +
                "container. Also count containers that have no box. If the model's reading clearly disagrees with what " +
                "you see, mention it in the summary. Be brief and concrete.",
        messages: [{
          role: "user",
          content: [
            { type: "image", source: { type: "base64", media_type: "image/jpeg", data: image } },
            { type: "text", text: `Camera ${cal.frameW}x${cal.frameH} px. Boxes [x, y, w, h] in px, left to right: ` +
                                  JSON.stringify(cal.boxes) + `\nLevel model readings: ${JSON.stringify(readings)}` },
          ],
        }],
      });
      if (resp.stop_reason === "refusal") throw new Error("Claude declined to look at the picture");
      const text = resp.content.filter((b): b is Anthropic.Beta.BetaTextBlock => b.type === "text").map((b) => b.text).join("");
      const r = JSON.parse(text) as SetupResult;
      for (const c of r.containers) {
        if (c.number >= 1 && c.number <= n && c.name.trim()) {
          await ctx.runMutation(api.channels.rename, { number: c.number, contents: c.name.trim().slice(0, 24) });
        }
      }
      await say(r.summary, { name: "bench_setup", args: r });
    } catch (e) {
      await say(`Calibration saved: ${n} containers, but Claude couldn't check the picture (${(e as Error).message}). ` +
                "You can name them on the Bench page.");
    }
    return null;
  },
});
