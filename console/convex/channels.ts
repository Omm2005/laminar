import { mutation, query } from "./_generated/server";
import { v } from "convex/values";

export const list = query({
  args: {},
  handler: async (ctx) => await ctx.db.query("channels").withIndex("by_number").collect(),
});

export const get = query({
  args: { number: v.number() },
  handler: async (ctx, args) => await ctx.db.query("channels")
    .withIndex("by_number", (q) => q.eq("number", args.number)).first(),
});

export const upsert = mutation({
  args: {
    number: v.number(),
    contents: v.string(),
    currentMl: v.number(),
    targetMl: v.number(),
    capacityMl: v.number(),
  },
  handler: async (ctx, args) => {
    const existing = await ctx.db.query("channels")
      .withIndex("by_number", (q) => q.eq("number", args.number)).first();
    const row = { ...args, updatedAt: Date.now() };
    if (existing) await ctx.db.patch(existing._id, row);
    else await ctx.db.insert("channels", row);
  },
});

/** Add a pour's volume to the channel's currentMl (called from the pour pipeline). */
export const addPoured = mutation({
  args: { number: v.number(), ml: v.number() },
  handler: async (ctx, args) => {
    const existing = await ctx.db.query("channels")
      .withIndex("by_number", (q) => q.eq("number", args.number)).first();
    if (!existing) return;
    await ctx.db.patch(existing._id, {
      currentMl: existing.currentMl + args.ml,
      updatedAt: Date.now(),
    });
  },
});
