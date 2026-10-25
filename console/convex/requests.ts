import { mutation } from "./_generated/server";
import { v } from "convex/values";
import { internal } from "./_generated/api";

/** User submits a free-text pour request. We insert a `planning` job and kick off the planner. */
export const submit = mutation({
  args: {
    requestText: v.string(),
    requestedBy: v.optional(v.string()),
  },
  returns: v.id("jobs"),
  handler: async (ctx, args) => {
    const now = Date.now();
    const jobId = await ctx.db.insert("jobs", {
      requestText: args.requestText,
      targetMl: 0, // planner fills these in
      channel: 0,
      vial: 0,
      status: "planning",
      createdAt: now,
    });
    await ctx.db.insert("events", {
      t: now, kind: "info", source: "web",
      message: `job submitted: ${args.requestText}`,
      jobId,
    });
    // Fire-and-forget: planner runs in an action because it calls Anthropic.
    await ctx.scheduler.runAfter(0, internal.planner.plan, { jobId });
    return jobId;
  },
});
