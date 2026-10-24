import { mutation, query } from "./_generated/server";
import { v } from "convex/values";

export const record = mutation({
  args: {
    jobId: v.id("jobs"),
    targetMl: v.number(),
    pouredMl: v.number(),
    onsetDeg: v.number(),
    pourDeg: v.number(),
    measuredMlPerS: v.number(),
    snapshotBefore: v.optional(v.id("_storage")),
    snapshotAfter: v.optional(v.id("_storage")),
  },
  handler: async (ctx, args) => {
    const id = await ctx.db.insert("pours", {
      jobId: args.jobId,
      targetMl: args.targetMl,
      pouredMl: args.pouredMl,
      errorMl: args.pouredMl - args.targetMl,
      onsetDeg: args.onsetDeg,
      pourDeg: args.pourDeg,
      measuredMlPerS: args.measuredMlPerS,
      snapshotBefore: args.snapshotBefore,
      snapshotAfter: args.snapshotAfter,
      createdAt: Date.now(),
    });
    return id;
  },
});

export const recent = query({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    return await ctx.db.query("pours")
      .withIndex("by_createdAt").order("desc").take(args.limit ?? 50);
  },
});

export const forJob = query({
  args: { jobId: v.id("jobs") },
  handler: async (ctx, args) => {
    return await ctx.db.query("pours")
      .withIndex("by_jobId", (q) => q.eq("jobId", args.jobId)).collect();
  },
});
