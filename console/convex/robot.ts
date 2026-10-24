import { mutation, query } from "./_generated/server";
import { v } from "convex/values";

/** Robot pushes a batch of telemetry samples. */
export const pushTelemetry = mutation({
  args: {
    jobId: v.optional(v.id("jobs")),
    samples: v.array(v.object({
      t: v.number(),
      joints: v.array(v.number()),
      gripperTorque: v.optional(v.number()),
      pourDeg: v.optional(v.number()),
    })),
  },
  handler: async (ctx, args) => {
    for (const s of args.samples) {
      await ctx.db.insert("telemetry", {
        jobId: args.jobId,
        t: s.t,
        joints: s.joints,
        gripperTorque: s.gripperTorque,
        pourDeg: s.pourDeg,
      });
    }
    return args.samples.length;
  },
});

/** Robot pushes a batch of level readings. */
export const pushLevels = mutation({
  args: {
    jobId: v.optional(v.id("jobs")),
    samples: v.array(v.object({
      t: v.number(),
      ml: v.number(),
      channel: v.optional(v.number()),
    })),
  },
  handler: async (ctx, args) => {
    for (const s of args.samples) {
      await ctx.db.insert("levels", {
        jobId: args.jobId,
        t: s.t,
        ml: s.ml,
        channel: s.channel,
      });
    }
    return args.samples.length;
  },
});

/** Latest telemetry sample, for dashboard gauges. */
export const latestTelemetry = query({
  args: { jobId: v.optional(v.id("jobs")) },
  handler: async (ctx, args) => {
    if (args.jobId) {
      return await ctx.db.query("telemetry")
        .withIndex("by_jobId_t", (q) => q.eq("jobId", args.jobId!))
        .order("desc").first();
    }
    return await ctx.db.query("telemetry").withIndex("by_t").order("desc").first();
  },
});

/** Level timeseries for a job, for the pour chart. */
export const levelsForJob = query({
  args: { jobId: v.id("jobs"), limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    const limit = args.limit ?? 500;
    const rows = await ctx.db.query("levels")
      .withIndex("by_jobId_t", (q) => q.eq("jobId", args.jobId))
      .order("asc").take(limit);
    return rows;
  },
});
