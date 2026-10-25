import { v } from "convex/values";
import { internalMutation, internalQuery, mutation, query, type MutationCtx } from "./_generated/server";
import { internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { TASK } from "./schema";
import { check, expand, r2, type Task } from "./lib";
import { workflow } from "./exec";
import { limiter } from "./limits";
import type { WorkflowId } from "@convex-dev/workflow";

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
export async function createRun(ctx: MutationCtx, requestText: string, requestedBy: string, executor: "sim" | "bench" = "sim", via: Via = "web", structured = false) {
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
  handler: async (ctx, args) => createRun(ctx, args.request, args.requestedBy, args.executor ?? "sim", args.via ?? "web"),
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

export const approve = mutation({
  args: { id: v.id("runs"), by: v.string() },
  handler: async (ctx, { id, by }) => {
    const who = name(by);
    const run = await ctx.db.get(id);
    if (!run) throw new Error("That run no longer exists.");
    if (run.status !== "awaiting_approval") throw new Error(`Only a plan waiting for approval can be approved; this one is ${run.status.replace("_", " ")}.`);
    // Re-check at approval time: the bench may have changed since the plan was read back.
    const errors = check(run.tasks as Task[], await ctx.db.query("tubes").collect(), await ctx.db.query("wells").collect());
    if (errors.length) {
      await ctx.db.patch(id, { status: "rejected", errors });
      await log(ctx, "checker", "refused", "Bench changed after read-back. " + errors.join(" "), id);
      return;
    }
    await ctx.db.patch(id, { status: "approved", approvedBy: who });
    await log(ctx, who, "approve", `Approved ${run.moves?.length ?? 0} moves.`, id);
    if (run.executor === "sim") await ctx.scheduler.runAfter(0, internal.runs.startNext, {});
  },
});

export const cancel = mutation({
  args: { id: v.id("runs"), by: v.string(), reason: v.optional(v.string()) },
  handler: async (ctx, { id, by, reason }) => {
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
    }
    const patch: Record<string, unknown> = {};
    if (a.step !== undefined) patch.step = a.step;
    if (a.status) {
      patch.status = a.status;
      if (a.status === "running") patch.startedAt = Date.now();
      else patch.finishedAt = Date.now();
      if (a.reason) patch.abortReason = a.reason;
      await log(ctx, "bench", a.status, a.reason ?? `Bench reports ${a.status}.`, a.id);
    }
    await ctx.db.patch(a.id, patch);
  },
});
