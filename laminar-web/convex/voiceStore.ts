import { v } from "convex/values";
import { internalMutation, internalQuery } from "./_generated/server";
import { limiter } from "./limits";

/** Cache of synthesized phrases: text → audio in file storage. */
export const lookup = internalQuery({
  args: { key: v.string() },
  handler: async (ctx, { key }) => {
    const row = await ctx.db.query("voiceCache").withIndex("by_key", (q) => q.eq("key", key)).first();
    return row ? await ctx.storage.getUrl(row.storageId) : null;
  },
});

export const save = internalMutation({
  args: { key: v.string(), storageId: v.id("_storage") },
  handler: async (ctx, { key, storageId }) => {
    const dup = await ctx.db.query("voiceCache").withIndex("by_key", (q) => q.eq("key", key)).first();
    if (dup) { await ctx.storage.delete(storageId); return (await ctx.storage.getUrl(dup.storageId))!; }
    await ctx.db.insert("voiceCache", { key, storageId, t: Date.now() });
    return (await ctx.storage.getUrl(storageId))!;
  },
});

export const limit = internalMutation({
  args: { key: v.string() },
  handler: async (ctx, { key }) => { await limiter.limit(ctx, "voice", { key, throws: true }); },
});

/** Plan read-backs are one-off; keep a day of them. */
export const prune = internalMutation({
  args: {},
  handler: async (ctx) => {
    const old = await ctx.db.query("voiceCache").withIndex("by_t", (q) => q.lt("t", Date.now() - 24 * 3600_000)).take(100);
    for (const r of old) { await ctx.storage.delete(r.storageId); await ctx.db.delete(r._id); }
  },
});
