import { query } from "./_generated/server";
import { v } from "convex/values";

/** Most recent scale sample overall. */
export const latest = query({
  args: {},
  handler: async (ctx) => await ctx.db.query("scale").withIndex("by_t").order("desc").first(),
});

/** Scale samples tagged with this jobId. */
export const forJob = query({
  args: { jobId: v.id("jobs"), limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    return await ctx.db.query("scale")
      .withIndex("by_jobId_t", (q) => q.eq("jobId", args.jobId))
      .order("asc").take(args.limit ?? 400);
  },
});
