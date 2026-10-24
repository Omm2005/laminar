import { mutation, query } from "./_generated/server";
import { v } from "convex/values";

export const list = query({
  args: {},
  handler: async (ctx) => await ctx.db.query("vials").withIndex("by_slot").collect(),
});

/** Insert or update by slot (idempotent). */
export const upsert = mutation({
  args: {
    slot: v.number(),
    letter: v.string(),
    contents: v.string(),
    volumeMl: v.number(),
  },
  handler: async (ctx, args) => {
    const existing = await ctx.db.query("vials")
      .withIndex("by_slot", (q) => q.eq("slot", args.slot)).first();
    const row = { ...args, updatedAt: Date.now() };
    if (existing) await ctx.db.patch(existing._id, row);
    else await ctx.db.insert("vials", row);
  },
});

export const remove = mutation({
  args: { slot: v.number() },
  handler: async (ctx, args) => {
    const existing = await ctx.db.query("vials")
      .withIndex("by_slot", (q) => q.eq("slot", args.slot)).first();
    if (existing) await ctx.db.delete(existing._id);
  },
});
