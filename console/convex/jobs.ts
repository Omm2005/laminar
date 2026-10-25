import { mutation, query, internalMutation, internalQuery } from "./_generated/server";
import { v } from "convex/values";
import { internal } from "./_generated/api";
import { readiness } from "./station";

const JOB_STATUS = v.union(
  v.literal("queued"), v.literal("planning"), v.literal("awaiting_approval"),
  v.literal("approved"), v.literal("running"), v.literal("pouring"),
  v.literal("done"), v.literal("failed"), v.literal("stalled"), v.literal("estop"), v.literal("rejected"),
);

export const list = query({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    const limit = args.limit ?? 50;
    return await ctx.db.query("jobs").withIndex("by_createdAt").order("desc").take(limit);
  },
});

export const get = query({
  args: { jobId: v.id("jobs") },
  handler: async (ctx, args) => await ctx.db.get(args.jobId),
});

export const getInternal = internalQuery({
  args: { jobId: v.id("jobs") },
  handler: async (ctx, args) => await ctx.db.get(args.jobId),
});

/** A PERSON approves a plan (button, voice or typed). The agent has no way to call this. */
export const approve = mutation({
  args: { jobId: v.id("jobs"), approvedBy: v.string(), via: v.optional(v.string()) },
  handler: async (ctx, args) => {
    const job = await ctx.db.get(args.jobId);
    if (!job) throw new Error("job not found");
    if (job.status !== "awaiting_approval") {
      throw new Error(`cannot approve job in status ${job.status}`);
    }
    const who = args.approvedBy.trim();
    if (!who) throw new Error("say who is approving");
    const notReady = await readiness(ctx);   // never queue a plan for a robot that isn't there
    if (notReady) throw new Error(`Can't run it: ${notReady}.`);
    await ctx.db.patch(args.jobId, { status: "approved", approvedBy: who, approvedVia: args.via ?? "button" });
    await ctx.db.insert("events", {
      t: Date.now(), kind: "info", source: "web", jobId: args.jobId,
      message: `plan approved by ${who} (${args.via ?? "button"})`,
    });
    if (job.threadId) {
      await ctx.db.insert("messages", {
        threadId: job.threadId, role: "tool", createdAt: Date.now(),
        text: `Approved by ${who}${args.via === "voice" ? " by voice" : ""}. Sent to the robot.`,
        tool: { name: "approval", args: { jobId: args.jobId, approved: true, by: who } },
      });
    }
  },
});

export const reject = mutation({
  args: { jobId: v.id("jobs"), approvedBy: v.string(), reason: v.optional(v.string()), via: v.optional(v.string()) },
  handler: async (ctx, args) => {
    const job = await ctx.db.get(args.jobId);
    if (!job) throw new Error("job not found");
    if (job.status !== "awaiting_approval") throw new Error(`cannot reject job in status ${job.status}`);
    await ctx.db.patch(args.jobId, {
      status: "rejected",
      finishedAt: Date.now(),
      failReason: args.reason ?? "rejected",
    });
    if (job.threadId) {
      await ctx.db.insert("messages", {
        threadId: job.threadId, role: "tool", createdAt: Date.now(),
        text: `Rejected by ${args.approvedBy || "the technician"}. Nothing moved.`,
        tool: { name: "approval", args: { jobId: args.jobId, approved: false, by: args.approvedBy } },
      });
    }
    await ctx.db.insert("events", {
      t: Date.now(), kind: "warn", source: "web", jobId: args.jobId,
      message: `job rejected by ${args.approvedBy}: ${args.reason ?? "no reason"}`,
    });
  },
});

/** Oldest `approved` job, shaped small for the robot client. */
export const nextForRobot = query({
  args: {},
  handler: async (ctx) => {
    const job = await ctx.db
      .query("jobs")
      .withIndex("by_status", (q) => q.eq("status", "approved"))
      .order("asc")
      .first();
    if (!job) return null;
    return {
      jobId: job._id,
      targetMl: job.targetMl,
      channel: job.channel,
      vial: job.vial,
      plan: job.plan ?? [],
      pours: job.pours ?? null,
      approvedBy: job.approvedBy ?? null,
    };
  },
});

export const setStatus = mutation({
  args: {
    jobId: v.id("jobs"),
    status: JOB_STATUS,
    currentStep: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const patch: Record<string, unknown> = { status: args.status };
    if (args.currentStep !== undefined) patch.currentStep = args.currentStep;
    if (["done", "failed", "stalled"].includes(args.status)) patch.finishedAt = Date.now();
    await ctx.db.patch(args.jobId, patch);
    await ctx.db.insert("events", {
      t: Date.now(), kind: "info", source: "robot", jobId: args.jobId,
      message: `status -> ${args.status}${args.currentStep ? ` (step ${args.currentStep})` : ""}`,
    });
  },
});

/* -------------------- internals used by the planner / watchdog -------------------- */

export const applyPlanInternal = internalMutation({
  args: {
    jobId: v.id("jobs"),
    targetMl: v.number(),
    channel: v.number(),
    vial: v.number(),
    plan: v.array(v.object({ name: v.string(), args: v.optional(v.any()) })),
  },
  handler: async (ctx, args) => {
    await ctx.db.patch(args.jobId, {
      status: "awaiting_approval",
      targetMl: args.targetMl,
      channel: args.channel,
      vial: args.vial,
      plan: args.plan,
    });
    await ctx.db.insert("events", {
      t: Date.now(), kind: "info", source: "planner", jobId: args.jobId,
      message: `plan ready: ${args.targetMl.toFixed(2)} mL -> channel ${args.channel} from vial ${args.vial}`,
    });
  },
});

export const failInternal = internalMutation({
  args: { jobId: v.id("jobs"), reason: v.string() },
  handler: async (ctx, args) => {
    await ctx.db.patch(args.jobId, {
      status: "failed",
      finishedAt: Date.now(),
      failReason: args.reason,
    });
    await ctx.db.insert("events", {
      t: Date.now(), kind: "error", source: "planner", jobId: args.jobId,
      message: args.reason,
    });
  },
});

/** Watchdog: if a running job has had no telemetry in the last 10 s, mark it stalled. */
export const checkStalledInternal = internalMutation({
  args: { jobId: v.id("jobs") },
  handler: async (ctx, args) => {
    const job = await ctx.db.get(args.jobId);
    if (!job) return;
    if (job.status !== "running" && job.status !== "pouring") return;
    const cutoff = Date.now() - 10_000;
    const last = await ctx.db
      .query("telemetry")
      .withIndex("by_jobId_t", (q) => q.eq("jobId", args.jobId).gt("t", cutoff))
      .first();
    if (last) return;
    await ctx.db.patch(args.jobId, {
      status: "stalled",
      finishedAt: Date.now(),
      failReason: "no telemetry for 10 s",
    });
    await ctx.db.insert("events", {
      t: Date.now(), kind: "error", source: "watchdog", jobId: args.jobId,
      message: "stalled: no telemetry for 10 s while running",
    });
  },
});

/* -------------------- agent plans (agent.ts) and the robot runner (bridge/run_plan.py) -------------------- */

/** The plan waiting for a person, newest first (null if none). */
export const pending = query({
  args: {},
  handler: async (ctx) => await ctx.db.query("jobs")
    .withIndex("by_status", (q) => q.eq("status", "awaiting_approval")).order("desc").first(),
});

/** The job the robot is on now, or the latest one. */
export const current = query({
  args: {},
  handler: async (ctx) => await ctx.db.query("jobs").withIndex("by_createdAt").order("desc").first(),
});

/** submit_plan: the checked plan is stored, NOT yet in front of anyone ("queued"). */
export const createPlanInternal = internalMutation({
  args: {
    threadId: v.string(),
    requestText: v.string(),
    pours: v.array(v.object({ vial: v.number(), container: v.number(), ml: v.number() })),
    plan: v.array(v.object({ name: v.string(), args: v.optional(v.any()), label: v.optional(v.string()),
                             placeholder: v.optional(v.boolean()) })),
  },
  handler: async (ctx, args) => {
    const first = args.pours[0];
    return await ctx.db.insert("jobs", {
      requestText: args.requestText, threadId: args.threadId,
      targetMl: first?.ml ?? 0, channel: first?.container ?? 0, vial: first?.vial ?? 0,
      status: "queued", createdAt: Date.now(),
      plan: args.plan, pours: args.pours,
    });
  },
});

/** request_approval: the plan goes in front of a person; any older plan still waiting is replaced. */
export const requestApprovalInternal = internalMutation({
  args: { jobId: v.id("jobs") },
  handler: async (ctx, args) => {
    const waiting = await ctx.db.query("jobs")
      .withIndex("by_status", (q) => q.eq("status", "awaiting_approval")).collect();
    for (const w of waiting) {
      if (w._id !== args.jobId) {
        await ctx.db.patch(w._id, { status: "rejected", finishedAt: Date.now(), failReason: "replaced by a newer plan" });
      }
    }
    await ctx.db.patch(args.jobId, { status: "awaiting_approval" });
  },
});

/** The robot runner reports the end of a job; the agent then explains it in the chat. */
export const finish = mutation({
  args: {
    jobId: v.id("jobs"),
    status: JOB_STATUS,
    failReason: v.optional(v.string()),
    report: v.optional(v.any()),
  },
  handler: async (ctx, args) => {
    const job = await ctx.db.get(args.jobId);
    if (!job) return;
    await ctx.db.patch(args.jobId, {
      status: args.status, finishedAt: Date.now(), report: args.report,
      failReason: args.failReason, currentStep: undefined,
    });
    await ctx.db.insert("events", {
      t: Date.now(), kind: args.status === "done" ? "info" : "error", source: "robot", jobId: args.jobId,
      message: `job ${args.status}${args.failReason ? `: ${args.failReason}` : ""}`,
    });
    if (job.threadId) {
      await ctx.scheduler.runAfter(0, internal.agent.explain, { jobId: args.jobId });
    }
  },
});

export const setSummaryInternal = internalMutation({
  args: { jobId: v.id("jobs"), summary: v.string() },
  handler: async (ctx, args) => { await ctx.db.patch(args.jobId, { summary: args.summary }); },
});

/** Oldest approved AGENT plan (it has pours and executor steps), for bridge/run_plan.py. */
export const nextPlanForRobot = query({
  args: {},
  handler: async (ctx) => {
    const approved = await ctx.db.query("jobs")
      .withIndex("by_status", (q) => q.eq("status", "approved")).order("asc").collect();
    const job = approved.find((j) => j.pours && j.plan?.length);
    if (!job) return null;
    return { jobId: job._id, plan: job.plan, pours: job.pours, approvedBy: job.approvedBy ?? null };
  },
});
