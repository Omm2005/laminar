import { mutation, query } from "./_generated/server";
import { v } from "convex/values";

/** Robot pushes a batch of telemetry samples. */
export const pushTelemetry = mutation({
  args: {
    jobId: v.optional(v.id("jobs")),
    samples: v.array(v.object({
      t: v.number(),
      joints: v.array(v.number()),                 // measured, for gauges + 3D model
      commanded: v.optional(v.array(v.number())),  // target (optional)
      gripperDeg: v.optional(v.number()),          // gripper angle in degrees from zero
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
        commanded: s.commanded,
        gripperDeg: s.gripperDeg,
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

/** Latest telemetry sample overall, regardless of jobId. Live-view gauges want the
    newest numbers from the arm no matter which job (if any) owns them. */
export const latestTelemetry = query({
  args: {},
  handler: async (ctx) => {
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
