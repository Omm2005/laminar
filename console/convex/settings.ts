import { mutation, query } from "./_generated/server";
import { v } from "convex/values";

export const get = query({
  args: { key: v.string() },
  handler: async (ctx, args) => {
    const row = await ctx.db.query("settings")
      .withIndex("by_key", (q) => q.eq("key", args.key)).first();
    return row?.value ?? null;
  },
});

export const list = query({
  args: {},
  handler: async (ctx) => await ctx.db.query("settings").collect(),
});

export const set = mutation({
  args: { key: v.string(), value: v.any() },
  handler: async (ctx, args) => {
    const row = await ctx.db.query("settings")
      .withIndex("by_key", (q) => q.eq("key", args.key)).first();
    if (row) await ctx.db.patch(row._id, { value: args.value, updatedAt: Date.now() });
    else await ctx.db.insert("settings", { ...args, updatedAt: Date.now() });
  },
});
