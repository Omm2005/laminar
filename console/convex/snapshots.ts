import { mutation } from "./_generated/server";
import { v } from "convex/values";

/** The frontend / robot asks for an upload URL, PUTs the image bytes, then calls attach. */
export const generateUploadUrl = mutation({
  args: {},
  handler: async (ctx) => await ctx.storage.generateUploadUrl(),
});

/** After upload, attach the storage id to a pour row (before or after shot). */
export const attach = mutation({
  args: {
    pourId: v.id("pours"),
    which: v.union(v.literal("before"), v.literal("after")),
    storageId: v.id("_storage"),
  },
  handler: async (ctx, args) => {
    const patch: Record<string, unknown> = {};
    if (args.which === "before") patch.snapshotBefore = args.storageId;
    else patch.snapshotAfter = args.storageId;
    await ctx.db.patch(args.pourId, patch);
  },
});
