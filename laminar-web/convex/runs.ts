import { v } from "convex/values";
import { internalMutation, internalQuery, mutation, query, type MutationCtx } from "./_generated/server";
import { internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { TASK } from "./schema";
import { check, describe, expand, r2, type Task } from "./lib";
import { workflow } from "./exec";
import { limiter } from "./limits";
import type { WorkflowId } from "@convex-dev/workflow";

/** Where approved runs go. "bench" = the real arm: robotics/bench_runner.py on the bench laptop takes them from
 *  /bench/next and reports back (/bench/update). "sim" = the simulated bench (exec.ts). */
export const EXECUTOR = "bench" as const;

export async function log(ctx: MutationCtx, actor: string, action: string, detail: string, runId?: Id<"runs">) {
  await ctx.db.insert("audit", { t: Date.now(), actor, action, detail, runId });
  await ctx.scheduler.runAfter(0, internal.tiger.ingestEvent, { t: Date.now(), runId: runId ?? null, actor, action, detail });
}

const name = (s: string) => {
  const n = s.trim();
  if (n.length < 2) throw new Error("Enter your name so the audit log records who did this.");
  return n.slice(0, 40);
};

// ---------------------------------------------------------------- queries

export const list = query({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, { limit }) =>
    ctx.db.query("runs").withIndex("by_createdAt").order("desc").take(Math.min(limit ?? 20, 100)),
});

export const get = query({
  args: { id: v.id("runs") },
  handler: async (ctx, { id }) => {
    const run = await ctx.db.get(id);
    if (!run) return null;
    const pours = await ctx.db.query("pours").withIndex("by_runId", (q) => q.eq("runId", id)).collect();
    return { run, pours };
  },
});

export const getInternal = internalQuery({
  args: { id: v.id("runs") },
  handler: async (ctx, { id }) => ctx.db.get(id),
});

// ---------------------------------------------------------------- lifecycle

/** A spoken or typed request becomes a run in `checking`; the planner action takes it from there. */
export type Via = "web" | "voice" | "mana" | "protocol";
export async function createRun(ctx: MutationCtx, requestText: string, requestedBy: string, executor: "sim" | "bench" = EXECUTOR, via: Via = "web", structured = false) {
  const who = name(requestedBy);
  await limiter.limit(ctx, via === "mana" ? "mana" : "submit", { key: via === "mana" ? undefined : who.toLowerCase(), throws: true });
  const request = requestText.trim().slice(0, 500);
  if (!request) throw new Error("Type or say what you want the arm to do.");
  const active = await ctx.db.query("runs").withIndex("by_status", (q) => q.eq("status", "running")).first();
  const id = await ctx.db.insert("runs", { request, requestedBy: who, status: "checking", executor, createdAt: Date.now(), structured, via });
  await log(ctx, who, "submit", request + (via !== "web" ? ` (via ${via === "mana" ? "Mana console" : via})` : "") + (active ? " (another run is still going; this one will wait)" : ""), id);
  await ctx.scheduler.runAfter(0, internal.planner.plan, { id });
  return id;
}

export const submit = mutation({
  args: { request: v.string(), requestedBy: v.string(), executor: v.optional(v.union(v.literal("sim"), v.literal("bench"))), via: v.optional(v.union(v.literal("web"), v.literal("voice"))) },
  handler: async (ctx, args) => createRun(ctx, args.request, args.requestedBy, args.executor ?? EXECUTOR, args.via ?? "web"),
});

/** Planner output lands here. The checker, not the model, decides whether it can be read back. */
export const applyPlan = internalMutation({
  args: {
    id: v.id("runs"), planner: v.string(), note: v.string(),
    tasks: v.optional(v.array(TASK)), question: v.optional(v.string()),
  },
  handler: async (ctx, a) => {
    const run = await ctx.db.get(a.id);
    if (!run || run.status !== "checking") return;
    if (a.question || !a.tasks) {
      await ctx.db.patch(a.id, { status: "rejected", planner: a.planner, plannerNote: a.note, question: a.question ?? "Could you rephrase that?" });
      await log(ctx, "planner", "question", a.question ?? a.note, a.id);
      return;
    }
    const tubes = await ctx.db.query("tubes").collect();
    const wells = await ctx.db.query("wells").collect();
    const errors = check(a.tasks as Task[], tubes, wells);
    await ctx.db.patch(a.id, {
      planner: a.planner, plannerNote: a.note, tasks: a.tasks, moves: expand(a.tasks as Task[]),
      errors, status: errors.length ? "rejected" : "awaiting_approval",
    });
    await log(ctx, "checker", errors.length ? "refused" : "passed",
      errors.length ? errors.join(" ") : `${a.tasks.length} tasks, ${expand(a.tasks as Task[]).length} moves. Waiting for approval.`, a.id);
  },
});

/** Shared by the Run tab, voice and the assistant chat: a named person approves a plan that is waiting. */
export async function approveRun(ctx: MutationCtx, id: Id<"runs">, by: string) {
  const who = name(by);
  const run = await ctx.db.get(id);
  if (!run) throw new Error("That run no longer exists.");
  if (run.status !== "awaiting_approval") throw new Error(`Only a plan waiting for approval can be approved; this one is ${run.status.replace("_", " ")}.`);
  // Re-check at approval time: the bench may have changed since the plan was read back.
  const errors = check(run.tasks as Task[], await ctx.db.query("tubes").collect(), await ctx.db.query("wells").collect());
  if (errors.length) {
    await ctx.db.patch(id, { status: "rejected", errors });
    await log(ctx, "checker", "refused", "Bench changed after read-back. " + errors.join(" "), id);
    return { ok: false as const, errors };
  }
  await ctx.db.patch(id, { status: "approved", approvedBy: who });
  await log(ctx, who, "approve", `Approved ${run.moves?.length ?? 0} moves.`, id);
  if (run.executor === "sim") await ctx.scheduler.runAfter(0, internal.runs.startNext, {});
  else await log(ctx, "bench", "queued", "Sent to the real arm: the bench laptop (bench_runner.py) picks it up.", id);
  return { ok: true as const, errors: [] };
}

export async function cancelRun(ctx: MutationCtx, id: Id<"runs">, by: string, reason?: string) {
  const who = name(by);
  const run = await ctx.db.get(id);
  if (!run) return;
  if (run.status === "done" || run.status === "aborted" || run.status === "rejected") return;
  const why = reason?.trim() || (run.status === "running" ? "Stopped by operator" : "Cancelled before motion");
  await ctx.db.patch(id, { status: "aborted", abortReason: why, finishedAt: Date.now() });
  await log(ctx, who, run.status === "running" ? "stop" : "cancel", why, id);
  // Stop the durable workflow; its onComplete handler starts the next queued run.
  if (run.workflowId) await workflow.cancel(ctx, run.workflowId as WorkflowId);
  else await ctx.scheduler.runAfter(0, internal.runs.startNext, {});
}

export const approve = mutation({
  args: { id: v.id("runs"), by: v.string() },
  handler: async (ctx, { id, by }) => { await approveRun(ctx, id, by); },
});

export const cancel = mutation({
  args: { id: v.id("runs"), by: v.string(), reason: v.optional(v.string()) },
  handler: async (ctx, { id, by, reason }) => cancelRun(ctx, id, by, reason),
});

/** A person changes a plan that is waiting for approval (tube, well, mL per pour). The checker runs again, in code; a
 *  plan that fails is not saved. The change is logged under their name and noted in the assistant chat. */
export const editPlan = mutation({
  args: { id: v.id("runs"), tasks: v.array(TASK), by: v.string() },
  handler: async (ctx, { id, tasks, by }): Promise<{ ok: boolean; errors: string[] }> => {
    const who = name(by);
    const run = await ctx.db.get(id);
    if (!run) throw new Error("That run no longer exists.");
    if (run.status !== "awaiting_approval") throw new Error(`Only a plan waiting for approval can be changed; this one is ${run.status.replace("_", " ")}.`);
    // Sample A goes first so reagents land on sample; the rest keep the order they were given.
    const ordered = [...(tasks as Task[])].sort((a, b) => (a.tube === "A" ? -1 : b.tube === "A" ? 1 : 0));
    const errors = check(ordered, await ctx.db.query("tubes").collect(), await ctx.db.query("wells").collect());
    if (errors.length) return { ok: false, errors };
    const moves = expand(ordered);
    await ctx.db.patch(id, { tasks: ordered, moves, editedBy: who, errors: [] });
    const what = describe(ordered);
    await log(ctx, who, "edit", `Changed the plan before approval: ${what}`, id);
    if (run.threadId) await ctx.db.insert("messages", { threadId: run.threadId, role: "user", text: `I changed the plan: ${what}`, by: who, t: Date.now() });
    return { ok: true, errors: [] };
  },
});

/** The assistant's request_approval (agent.ts): a plan Claude proposed becomes a run waiting for a person. The
 *  checker runs again here, in code; an older plan still waiting in the same conversation is replaced. */
export const proposeFromChat = internalMutation({
  args: { threadId: v.string(), request: v.string(), requestedBy: v.string(), planner: v.string(), note: v.string(), tasks: v.array(TASK) },
  handler: async (ctx, a): Promise<{ ok: true; runId: Id<"runs"> } | { ok: false; errors: string[] }> => {
    const tasks = a.tasks as Task[];
    const errors = check(tasks, await ctx.db.query("tubes").collect(), await ctx.db.query("wells").collect());
    if (errors.length) return { ok: false, errors };
    const waiting = await ctx.db.query("runs").withIndex("by_status", (q) => q.eq("status", "awaiting_approval"))
      .filter((q) => q.eq(q.field("threadId"), a.threadId)).collect();
    for (const old of waiting) {
      await ctx.db.patch(old._id, { status: "rejected", errors: ["Replaced by a newer plan in the same conversation."] });
      await log(ctx, "planner", "replaced", "A newer plan from the assistant replaced this one before approval.", old._id);
    }
    const who = name(a.requestedBy);
    const moves = expand(tasks);
    const id = await ctx.db.insert("runs", {
      request: a.request.trim().slice(0, 500) || a.note, requestedBy: who, status: "awaiting_approval", executor: EXECUTOR,
      planner: a.planner, plannerNote: a.note, tasks, moves, errors: [], via: "chat", threadId: a.threadId, createdAt: Date.now(),
    });
    await log(ctx, who, "submit", `${a.request.trim().slice(0, 300)} (via assistant chat)`, id);
    await log(ctx, "checker", "passed", `${tasks.length} tasks, ${moves.length} moves. Waiting for approval.`, id);
    return { ok: true, runId: id };
  },
});

// ---------------------------------------------------------------- simulated executor (durable workflow, exec.ts)

/** Start the oldest approved sim run if nothing is running. */
export const startNext = internalMutation({
  args: {},
  handler: async (ctx) => {
    if (await ctx.db.query("runs").withIndex("by_status", (q) => q.eq("status", "running")).first()) return;
    const next = await ctx.db.query("runs").withIndex("by_status", (q) => q.eq("status", "approved"))
      .filter((q) => q.eq(q.field("executor"), "sim")).first();
    if (!next) return;
    const workflowId = await workflow.start(ctx, internal.exec.runAssay, { runId: next._id }, {
      onComplete: internal.exec.onWorkflowDone, context: { runId: next._id },
    });
    await ctx.db.patch(next._id, { status: "running", step: 0, startedAt: Date.now(), workflowId });
    await log(ctx, "executor", "start", "Simulated bench: replaying moves as a durable workflow.", next._id);
  },
});

export const setSummary = internalMutation({
  args: { id: v.id("runs"), summary: v.string() },
  handler: async (ctx, { id, summary }) => {
    await ctx.db.patch(id, { summary });
    await log(ctx, "planner", "report", summary, id);
  },
});

// ---------------------------------------------------------------- bench executor (HTTP, see http.ts)

export const nextForBench = internalQuery({
  args: {},
  handler: async (ctx) => {
    const r = await ctx.db.query("runs").withIndex("by_status", (q) => q.eq("status", "approved"))
      .filter((q) => q.eq(q.field("executor"), "bench")).first();
    return r ? { id: r._id, tasks: r.tasks, moves: r.moves } : null;
  },
});

export const benchUpdate = internalMutation({
  args: {
    id: v.id("runs"),
    status: v.optional(v.union(v.literal("running"), v.literal("done"), v.literal("aborted"))),
    step: v.optional(v.number()),
    reason: v.optional(v.string()),
    pour: v.optional(v.object({ tube: v.union(v.literal("A"), v.literal("B"), v.literal("C"), v.literal("D")), well: v.number(), targetMl: v.number(), pouredMl: v.number() })),
  },
  handler: async (ctx, a) => {
    const run = await ctx.db.get(a.id);
    if (!run || run.executor !== "bench") throw new Error("not a bench run");
    if (a.pour) {
      const t = Date.now(), err = r2(a.pour.pouredMl - a.pour.targetMl);
      await ctx.db.insert("pours", { runId: a.id, ...a.pour, errorMl: err, source: "bench", t });
      await ctx.scheduler.runAfter(0, internal.tiger.ingestPour, { t, runId: a.id, ...a.pour, errorMl: err, source: "bench" });
      // the bench state follows the measured pour, as with the simulated bench
      const poured = Math.max(0, a.pour.pouredMl);
      const tubeRow = await ctx.db.query("tubes").filter((q) => q.eq(q.field("letter"), a.pour!.tube)).first();
      if (tubeRow) await ctx.db.patch(tubeRow._id, { volumeMl: r2(Math.max(0, tubeRow.volumeMl - poured)), updatedAt: t });
      const w = await ctx.db.query("wells").withIndex("by_number", (q) => q.eq("number", a.pour!.well)).first();
      if (w) {
        const contents = [...w.contents];
        const c = contents.find((x) => x.tube === a.pour!.tube);
        if (c) c.ml = r2(c.ml + poured); else contents.push({ tube: a.pour.tube, ml: poured });
        await ctx.db.patch(w._id, { volumeMl: r2(w.volumeMl + poured), contents, updatedAt: t });
      }
      if (Math.abs(err) > 0.1 * a.pour.targetMl) {
        await log(ctx, "bench", "warn", `Pour into well ${a.pour.well} added ${poured.toFixed(2)} mL instead of ${a.pour.targetMl.toFixed(2)} mL.`, a.id);
      }
    }
    const patch: Record<string, unknown> = {};
    if (a.step !== undefined) patch.step = a.step;
    if (a.status) {
      patch.status = a.status;
      if (a.status === "running") patch.startedAt = Date.now();
      else patch.finishedAt = Date.now();
      if (a.reason) patch.abortReason = a.reason;
      if (a.status !== "running") {                       // the result line, from the measured pours
        const pours = await ctx.db.query("pours").withIndex("by_runId", (q) => q.eq("runId", a.id)).collect();
        const off = pours.filter((p) => Math.abs(p.errorMl) > 0.1 * p.targetMl);
        patch.result = [
          a.status === "aborted" ? `Stopped: ${a.reason ?? "the bench aborted the run"}.` : "",
          `${pours.length} pour${pours.length === 1 ? "" : "s"} on the real arm, ${pours.length - off.length} within ±10% of target.`,
          off.length ? `Check ${off.map((p) => `well ${p.well} (${p.pouredMl.toFixed(2)} of ${p.targetMl.toFixed(2)} mL)`).join(", ")}.` : "",
        ].filter(Boolean).join(" ");
      }
      await log(ctx, "bench", a.status, a.reason ?? `Bench reports ${a.status}.`, a.id);
    }
    await ctx.db.patch(a.id, patch);
    if (a.status === "done" || a.status === "aborted") await ctx.scheduler.runAfter(0, internal.planner.reflect, { id: a.id });
  },
});
