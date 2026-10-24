import { mutation, query } from "./_generated/server";
import { v } from "convex/values";

const ONLINE_WINDOW_MS = 10_000;

/** < 200-byte summary for the bench head unit. */
export const state = query({
  args: {},
  handler: async (ctx) => {
    const job = await ctx.db.query("jobs")
      .withIndex("by_createdAt").order("desc").first();
    if (!job) {
      return { status: "idle" as const, step: null, targetMl: 0, pouredMl: 0, needsApproval: false };
    }
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
  args: { name: v.string() },
  handler: async (ctx, args) => {
    const job = await ctx.db.query("jobs")
      .withIndex("by_status", (q) => q.eq("status", "awaiting_approval"))
      .order("asc").first();
    if (!job) return { ok: false, reason: "no awaiting-approval job" };
    await ctx.db.patch(job._id, { status: "approved", approvedBy: `bench:${args.name}` });
    await ctx.db.insert("events", {
      t: Date.now(), kind: "info", source: "bench", jobId: job._id,
      message: `approved by bench ${args.name}`,
    });
    return { ok: true, jobId: job._id };
  },
});

export const heartbeat = mutation({
  args: { name: v.string(), button: v.optional(v.string()) },
  handler: async (ctx, args) => {
    const existing = await ctx.db.query("benchNodes")
      .withIndex("by_name", (q) => q.eq("name", args.name)).first();
    const now = Date.now();
    const patch: Record<string, unknown> = { lastHeartbeat: now, online: true };
    if (args.button !== undefined) patch.lastButton = args.button;
    if (existing) {
      await ctx.db.patch(existing._id, patch);
    } else {
      await ctx.db.insert("benchNodes", {
        name: args.name,
        lastHeartbeat: now,
        online: true,
        lastButton: args.button,
      });
    }
  },
});

export const pushScale = mutation({
  args: {
    grams: v.number(),
    t: v.optional(v.number()),
    jobId: v.optional(v.id("jobs")),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("scale", {
      t: args.t ?? Date.now(),
      grams: args.grams,
      jobId: args.jobId,
    });
  },
});

/** Returns bench nodes with `online` recomputed fresh (heartbeat within 10 s). */
export const nodes = query({
  args: {},
  handler: async (ctx) => {
    const now = Date.now();
    const rows = await ctx.db.query("benchNodes").collect();
    return rows.map((n) => ({ ...n, online: now - n.lastHeartbeat < ONLINE_WINDOW_MS }));
  },
});
