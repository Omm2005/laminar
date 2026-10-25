import { mutation, query } from "./_generated/server";
import { v } from "convex/values";

export const log = mutation({
  args: {
    kind: v.string(),
    source: v.string(),
    message: v.string(),
    jobId: v.optional(v.id("jobs")),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("events", {
      t: Date.now(),
      kind: args.kind,
      source: args.source,
      message: args.message,
      jobId: args.jobId,
    });
  },
});

export const recent = query({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    return await ctx.db.query("events").withIndex("by_t").order("desc").take(args.limit ?? 50);
  },
});
