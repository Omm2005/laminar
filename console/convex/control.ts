import { mutation, query } from "./_generated/server";
import { v } from "convex/values";

/** True if an `estop` event is newer than the last `clear` event. */
export const stopRequested = query({
  args: {},
  handler: async (ctx) => {
    const lastEstop = await ctx.db.query("events")
      .withIndex("by_kind_t", (q) => q.eq("kind", "estop"))
      .order("desc").first();
    if (!lastEstop) return { stop: false, since: null };
    const lastClear = await ctx.db.query("events")
      .withIndex("by_kind_t", (q) => q.eq("kind", "clear"))
      .order("desc").first();
    const stop = !lastClear || lastClear.t < lastEstop.t;
    return { stop, since: stop ? lastEstop.t : null };
  },
});

export const estop = mutation({
  args: { source: v.string(), message: v.optional(v.string()) },
  handler: async (ctx, args) => {
    await ctx.db.insert("events", {
      t: Date.now(), kind: "estop", source: args.source,
      message: args.message ?? "E-STOP",
    });
  },
});

export const clearStop = mutation({
  args: { source: v.string() },
  handler: async (ctx, args) => {
    await ctx.db.insert("events", {
      t: Date.now(), kind: "clear", source: args.source,
      message: "stop cleared",
    });
  },
});
