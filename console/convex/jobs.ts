import { mutation, query, internalMutation, internalQuery } from "./_generated/server";
import { v } from "convex/values";

const JOB_STATUS = v.union(
  v.literal("queued"), v.literal("planning"), v.literal("awaiting_approval"),
  v.literal("approved"), v.literal("running"), v.literal("pouring"),
  v.literal("done"), v.literal("failed"), v.literal("stalled"), v.literal("estop"),
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

export const approve = mutation({
  args: { jobId: v.id("jobs"), approvedBy: v.string() },
  handler: async (ctx, args) => {
    const job = await ctx.db.get(args.jobId);
    if (!job) throw new Error("job not found");
    if (job.status !== "awaiting_approval") {
      throw new Error(`cannot approve job in status ${job.status}`);
    }
    await ctx.db.patch(args.jobId, { status: "approved", approvedBy: args.approvedBy });
    await ctx.db.insert("events", {
      t: Date.now(), kind: "info", source: "web", jobId: args.jobId,
      message: `job approved by ${args.approvedBy}`,
    });
  },
});

export const reject = mutation({
  args: { jobId: v.id("jobs"), approvedBy: v.string(), reason: v.optional(v.string()) },
  handler: async (ctx, args) => {
    const job = await ctx.db.get(args.jobId);
    if (!job) throw new Error("job not found");
    await ctx.db.patch(args.jobId, {
      status: "failed",
      finishedAt: Date.now(),
      failReason: args.reason ?? "rejected",
    });
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
