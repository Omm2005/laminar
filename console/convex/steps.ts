import { mutation, query } from "./_generated/server";
import { v } from "convex/values";
import { internal } from "./_generated/api";

export const start = mutation({
  args: { jobId: v.id("jobs"), name: v.string() },
  handler: async (ctx, args) => {
    const stepId = await ctx.db.insert("steps", {
      jobId: args.jobId, name: args.name, startedAt: Date.now(),
    });
    await ctx.db.patch(args.jobId, { currentStep: args.name });
    // Schedule the stall watchdog 11 s out. If telemetry has arrived by then it's a no-op.
    await ctx.scheduler.runAfter(11_000, internal.jobs.checkStalledInternal, { jobId: args.jobId });
    return stepId;
  },
});

export const end = mutation({
  args: {
    stepId: v.id("steps"),
    ok: v.boolean(),
    reason: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    await ctx.db.patch(args.stepId, {
      endedAt: Date.now(), ok: args.ok, reason: args.reason,
    });
  },
});

export const forJob = query({
  args: { jobId: v.id("jobs") },
  handler: async (ctx, args) => {
    return await ctx.db.query("steps")
      .withIndex("by_jobId", (q) => q.eq("jobId", args.jobId))
      .order("asc").collect();
  },
});
