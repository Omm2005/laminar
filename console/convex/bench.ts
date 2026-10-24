import { mutation, query } from "./_generated/server";
import { v } from "convex/values";

/** < 200-byte summary for the bench head unit. */
export const state = query({
  args: {},
  handler: async (ctx) => {
    const job = await ctx.db.query("jobs")
      .withIndex("by_createdAt").order("desc").first();
    if (!job) {
      return { status: "idle" as const, step: null, targetMl: 0, pouredMl: 0, needsApproval: false };
    }
    // most recent recorded pour for this job (if any) gives the current pouredMl
    const lastPour = await ctx.db.query("pours")
      .withIndex("by_jobId", (q) => q.eq("jobId", job._id)).order("desc").first();
    return {
      status: job.status,
      step: job.currentStep ?? null,
      targetMl: job.targetMl,
      pouredMl: lastPour?.pouredMl ?? 0,
      needsApproval: job.status === "awaiting_approval",
    };
  },
});

export const approve = mutation({
  args: { nodeName: v.string() },
  handler: async (ctx, args) => {
    const job = await ctx.db.query("jobs")
      .withIndex("by_status", (q) => q.eq("status", "awaiting_approval"))
      .order("asc").first();
    if (!job) return { ok: false, reason: "no awaiting-approval job" };
    await ctx.db.patch(job._id, { status: "approved", approvedBy: `bench:${args.nodeName}` });
    await ctx.db.insert("events", {
      t: Date.now(), kind: "info", source: "bench", jobId: job._id,
      message: `approved by bench ${args.nodeName}`,
    });
    return { ok: true, jobId: job._id };
  },
});

export const heartbeat = mutation({
  args: { nodeName: v.string(), button: v.optional(v.string()) },
  handler: async (ctx, args) => {
    const existing = await ctx.db.query("benchNodes")
      .withIndex("by_name", (q) => q.eq("name", args.nodeName)).first();
    const now = Date.now();
    if (existing) {
      await ctx.db.patch(existing._id, { lastHeartbeat: now, lastButton: args.button });
    } else {
      await ctx.db.insert("benchNodes", {
        name: args.nodeName, lastHeartbeat: now, lastButton: args.button,
      });
    }
  },
});

export const nodes = query({
  args: {},
  handler: async (ctx) => await ctx.db.query("benchNodes").collect(),
});
