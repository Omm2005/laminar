import { query } from "./_generated/server";
import { v } from "convex/values";

export const getUrl = query({
  args: { storageId: v.id("_storage") },
  handler: async (ctx, args) => await ctx.storage.getUrl(args.storageId),
});
