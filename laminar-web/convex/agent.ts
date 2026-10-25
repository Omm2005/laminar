"use node";
/**
 * The assistant chat on the console (ported from the console-agent branch). Claude talks with the technician and may
 * call the PRD's four tools:
 *    get_bench_state  - tubes A-D, wells 1-4, the limits, what is running
 *    submit_plan      - tasks [{tube, pours: [{well, ml}]}]; the CHECKER (lib.ts, plain code) answers ok or the errors
 *    request_approval - a checked plan becomes a run waiting for a person, shown in the chat as a plan card
 *    get_run_report   - what a run did: each pour against its target, why it stopped
 * There is no approve tool: approve, cancel and stop come only from a named person (messages.decide, or the Run tab).
 * Without ANTHROPIC_API_KEY the rule planner in lib.ts answers instead, so the chat still works offline.
 */
import Anthropic from "@anthropic-ai/sdk";
import { v } from "convex/values";
import { internalAction, type ActionCtx } from "./_generated/server";
import { api, internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import { CONTROL_WELL, LOW_TUBE_ML, describe, MAX_A_ALIQUOTS, MAX_POUR_ML, MIN_POUR_ML, WELL_CAPACITY_ML, check, expand, rulePlan, type Task } from "./lib";

const MODEL = "claude-opus-5-5";
const MAX_TURNS = 8;
const HISTORY = 30;

const SYSTEM = `You are Laminar, the assistant for a lab robot arm that pours measured volumes of liquid. You talk with a
lab technician by chat or voice.

Bench: tubes A-D in a rack (A is the blood sample, B/C/D are reagents 1-3), wells 1-4 on a tray. Well ${CONTROL_WELL} is the
control and only ever gets sample A. The standard assay: A into wells 1, 2, 3, 4 at 1.0 mL each, then B into well 1,
C into well 2, D into well 3 at 0.5 mL each. A task is one tube picked up once with its list of pours.

The loop: read the request -> get_bench_state if you need volumes or what is running -> submit_plan (code checks it) ->
if it reports errors, fix the plan and resubmit, or explain why it can't be done -> request_approval (the technician
sees a plan card with Approve / Cancel) -> after a run, get_run_report to explain what happened.
You never start, approve or stop the robot and you never say a plan is approved or running unless a report says so.

- To change a plan that is waiting (more or less liquid, another well, drop a pour), submit the revised plan and call
  request_approval again; it replaces the old card. The technician can also edit a card by hand: the card in the
  history shows the plan as it is now.
- If the tube, the well or the volume is unclear, ask one short question. Never invent a volume.
- Plain questions (what's in the tubes, how did the last run go) need no plan.
- Replies are read aloud: one to three short sentences, plain words, no markdown.`;

const TASKS_SCHEMA = {
  type: "array",
  items: {
    type: "object",
    properties: {
      tube: { type: "string", enum: ["A", "B", "C", "D"] },
      pours: {
        type: "array",
        items: {
          type: "object",
          properties: { well: { type: "integer", description: "well number, 1 to 4" }, ml: { type: "number", description: "mL to pour into it" } },
          required: ["well", "ml"], additionalProperties: false,
        },
      },
    },
    required: ["tube", "pours"], additionalProperties: false,
  },
};

const TOOLS: Anthropic.Beta.BetaTool[] = [
  {
    name: "get_bench_state",
    description: "Tubes (slot, letter, what it stands for, mL left, fill line), wells (mL, capacity, what is in them), " +
      "the pour limits, whether a run is going, and the last few runs.",
    input_schema: { type: "object", properties: {}, additionalProperties: false },
    strict: true,
  },
  {
    name: "submit_plan",
    description: "Submit tasks for the checker (plain code). Returns ok with a plan_id and the read-back, or the errors " +
      "to fix. A submitted plan is not shown to the technician until you call request_approval.",
    input_schema: {
      type: "object",
      properties: { tasks: TASKS_SCHEMA, note: { type: "string", description: "one plain sentence: what you understood, read back to the technician" } },
      required: ["tasks", "note"], additionalProperties: false,
    },
    strict: true,
  },
  {
    name: "request_approval",
    description: "Show a checked plan to the technician as a plan card (Approve / Cancel). Only a person can approve; " +
      "the robot does nothing until they do. Replaces an older plan still waiting in this conversation.",
    input_schema: { type: "object", properties: { plan_id: { type: "string" } }, required: ["plan_id"], additionalProperties: false },
    strict: true,
  },
  {
    name: "get_run_report",
    description: "What happened in a run: status, each pour's target and measured volume, the readout, why it stopped. " +
      "Leave run_id empty for the latest finished run.",
    input_schema: { type: "object", properties: { run_id: { type: "string" } }, required: ["run_id"], additionalProperties: false },
    strict: true,
  },
];

type Bench = { tubes: Doc<"tubes">[]; wells: Doc<"wells">[]; running: Doc<"runs"> | null };
type Draft = { tasks: Task[]; note: string };
const r2 = (x: number) => Math.round(x * 100) / 100;

function readBack(tasks: Task[]) {
  return tasks.map((t) => `Tube ${t.tube}: ${t.pours.map((p) => `${p.ml} mL into well ${p.well}`).join(", ")}.`).join(" ") +
    ` ${expand(tasks).length} moves.`;
}

function benchFacts(b: Bench, recent: Doc<"runs">[]) {
  return {
    tubes: b.tubes.map((t) => ({ slot: t.slot, tube: t.letter, stands_for: t.stands, ml: r2(t.volumeMl), fill_line_ml: t.fillLineMl, low: t.volumeMl < LOW_TUBE_ML })),
    wells: b.wells.map((w) => ({ well: w.number, ml: r2(w.volumeMl), capacity_ml: w.capacityMl, contents: w.contents, control: w.number === CONTROL_WELL })),
    limits: { pour_ml: [MIN_POUR_ML, MAX_POUR_ML], well_capacity_ml: WELL_CAPACITY_ML, max_tube_a_aliquots: MAX_A_ALIQUOTS,
              rack_order: "A, B, C, D left to right; a swapped rack is refused", sample_before_reagents: true },
    running: b.running ? { run_id: b.running._id, request: b.running.request, move: b.running.step, of: b.running.moves?.length } : null,
    recent_runs: recent.slice(0, 5).map((r) => ({ run_id: r._id, request: r.request, status: r.status, by: r.requestedBy })),
  };
}

async function report(ctx: ActionCtx, runId: string) {
  const recent = await ctx.runQuery(api.runs.list, { limit: 50 });
  const id = runId.trim();
  const run = id ? recent.find((r) => r._id === id || r._id.endsWith(id))
                 : recent.find((r) => r.status === "done" || r.status === "aborted");
  if (!run) return { found: false };
  const detail = await ctx.runQuery(api.runs.get, { id: run._id });
  return {
    found: true, run_id: run._id, request: run.request, status: run.status, approved_by: run.approvedBy ?? null,
    move: run.step ?? 0, of: run.moves?.length ?? 0, refused_because: run.errors?.length ? run.errors : null,
    stopped_because: run.abortReason ?? null, readout: run.result ?? null, note: run.summary ?? null,
    pours: (detail?.pours ?? []).map((p) => ({ tube: p.tube, well: p.well, target_ml: p.targetMl, poured_ml: p.pouredMl, error_ml: p.errorMl })),
  };
}

/** A plan card as Claude sees it in the history: the run as it is now. */
function cardText(run: Doc<"runs"> | null) {
  if (!run) return "[plan card: the run no longer exists]";
  return `[plan card for run ${run._id}: ${run.status.replace("_", " ")}` +
    (run.tasks?.length ? `; pours: ${describe(run.tasks as Task[])}` : "") +
    (run.editedBy ? ` (edited by ${run.editedBy})` : "") +
    (run.approvedBy ? `, approved by ${run.approvedBy}` : "") + (run.abortReason ? `, stopped: ${run.abortReason}` : "") +
    (run.result ? `, readout: ${run.result}` : "") + "]";
}

export const chat = internalAction({
  args: { threadId: v.string(), by: v.string() },
  handler: async (ctx, { threadId, by }) => {
    const say = (text: string, runId?: Id<"runs">) => ctx.runMutation(internal.messages.append, { threadId, text, runId });
    const rows = await ctx.runQuery(internal.messages.listInternal, { threadId, limit: HISTORY });
    const request = [...rows].reverse().find((m) => m.role === "user")?.text ?? "";
    const drafts = new Map<string, Draft>();
    let carded = false;

    const bench = async (): Promise<Bench> => ctx.runQuery(api.bench.state, {});
    const submit = async (tasks: Task[], note: string) => {
      const b = await bench();
      const errors = check(tasks, b.tubes, b.wells);
      if (errors.length) return { ok: false, errors };
      const planId = `p${drafts.size + 1}`;
      drafts.set(planId, { tasks, note });
      return { ok: true, plan_id: planId, read_back: readBack(tasks), run_is_going: !!b.running };
    };
    const requestApproval = async (planId: string, planner: string) => {
      const d = drafts.get(planId.trim());
      if (!d) return { ok: false, errors: ["No checked plan with that id in this turn: call submit_plan first."] };
      const r = await ctx.runMutation(internal.runs.proposeFromChat, { threadId, request, requestedBy: by, planner, note: d.note, tasks: d.tasks });
      if (!r.ok) return r;
      await say(d.note, r.runId);
      carded = true;
      return { ok: true, run_id: r.runId, shown: true, note: "The technician sees Approve / Cancel. It is NOT approved." };
    };

    try {
      if (!process.env.ANTHROPIC_API_KEY) {
        await say(await offline(ctx, request, submit, (id) => requestApproval(id, "rules"), bench));
        return null;
      }
      const client = new Anthropic();
      const messages: Anthropic.Beta.BetaMessageParam[] = history(rows);
      let final = "";
      for (let turn = 0; turn < MAX_TURNS; turn++) {
        const resp = await client.beta.messages.create({
          model: MODEL, max_tokens: 16000, system: SYSTEM, tools: TOOLS, messages,
          output_config: { effort: "low" },
          betas: ["server-side-fallback-2026-07-01"], fallbacks: "default",
        });
        if (resp.stop_reason === "refusal") { final = "I can't help with that request."; break; }
        messages.push({ role: "assistant", content: resp.content });     // thinking blocks go back unchanged
        final = resp.content.filter((b): b is Anthropic.Beta.BetaTextBlock => b.type === "text").map((b) => b.text).join("").trim();
        const uses = resp.content.filter((b): b is Anthropic.Beta.BetaToolUseBlock => b.type === "tool_use");
        if (resp.stop_reason !== "tool_use" || !uses.length) break;
        const results: Anthropic.Beta.BetaToolResultBlockParam[] = [];
        for (const u of uses) {
          const input = u.input as { tasks?: Task[]; note?: string; plan_id?: string; run_id?: string };
          let out: unknown;
          if (u.name === "get_bench_state") out = benchFacts(await bench(), await ctx.runQuery(api.runs.list, { limit: 5 }));
          else if (u.name === "submit_plan") out = await submit(input.tasks ?? [], String(input.note ?? "").slice(0, 300));
          else if (u.name === "request_approval") out = await requestApproval(String(input.plan_id ?? ""), MODEL);
          else if (u.name === "get_run_report") out = await report(ctx, String(input.run_id ?? ""));
          else out = { ok: false, errors: [`unknown tool ${u.name}`] };
          const failed = typeof out === "object" && out !== null && "ok" in out && (out as { ok: boolean }).ok === false;
          results.push({ type: "tool_result", tool_use_id: u.id, content: JSON.stringify(out), is_error: failed });
        }
        messages.push({ role: "user", content: results });
      }
      await say(final || (carded ? "Here's the plan. Approve or cancel it." : "Sorry, I don't have an answer for that."));
    } catch (e) {
      // The arm never depends on the model: answer with the rule planner and say why.
      const msg = e instanceof Anthropic.APIError ? `Claude is unavailable (${e.status ?? "network"})` : `The assistant hit an error (${(e as Error).message.slice(0, 80)})`;
      if (carded) await say(`${msg}. The plan above was already checked and is waiting for you.`);
      else await say(`${msg}. ${await offline(ctx, request, submit, (id) => requestApproval(id, "rules"), bench)}`);
    }
    return null;
  },
});

/** The thread as Claude sees it: text only, plan cards as their run's current state, roles alternating, user first. */
function history(rows: (Doc<"messages"> & { run: Doc<"runs"> | null })[]): Anthropic.Beta.BetaMessageParam[] {
  const out: { role: "user" | "assistant"; content: string }[] = [];
  for (const m of rows) {
    const content = m.runId ? `${m.text}\n${cardText(m.run)}` : m.text;
    const last = out[out.length - 1];
    if (last && last.role === m.role) last.content += "\n" + content;
    else out.push({ role: m.role, content });
  }
  while (out.length && out[0].role !== "user") out.shift();
  return out;
}

/** No API key (or Claude down): the deterministic rule planner, plus two plain questions. */
async function offline(
  ctx: ActionCtx, text: string,
  submit: (tasks: Task[], note: string) => Promise<{ ok: boolean; errors?: string[]; plan_id?: string }>,
  approve: (planId: string) => Promise<{ ok: boolean; errors?: string[] }>,
  bench: () => Promise<Bench>,
): Promise<string> {
  const t = text.toLowerCase();
  if (/\b(bench|tubes?|wells?|inventory|left)\b.*\?|what('s| is) (on|in|left)/.test(t) && !/\bpour\b/.test(t)) {
    const b = await bench();
    return `Tubes: ${b.tubes.map((x) => `${x.letter} ${x.stands} ${r2(x.volumeMl)} mL`).join(", ")}. ` +
      `Wells: ${b.wells.map((w) => `${w.number} has ${r2(w.volumeMl)} mL`).join(", ")}.` + (b.running ? " A run is going." : "");
  }
  if (/\b(why|what happened|stopped|last run|report)\b/.test(t) && !/\bpour\b/.test(t)) {
    const r = await report(ctx, "");
    if (!r.found) return "No finished runs yet.";
    return `The last run, "${r.request}", ended ${r.status}${r.stopped_because ? ` because ${r.stopped_because}` : ""}. ${r.readout ?? ""}`.trim();
  }
  const plan = rulePlan(text);
  if (!plan.tasks) return plan.question ?? "Could you rephrase that?";
  const s = await submit(plan.tasks, plan.note);
  if (!s.ok || !s.plan_id) return `I can't plan that: ${(s.errors ?? []).join(" ")}`;
  const a = await approve(s.plan_id);
  return a.ok ? "Here's the plan. Approve or cancel it." : `I can't plan that: ${(a.errors ?? []).join(" ")}`;
}
