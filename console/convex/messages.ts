import { mutation, query, internalMutation, internalQuery } from "./_generated/server";
import { v } from "convex/values";
import { internal } from "./_generated/api";

const DEFAULT_THREAD = "main";

export const list = query({
  args: { threadId: v.optional(v.string()), limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    const threadId = args.threadId ?? DEFAULT_THREAD;
    return await ctx.db.query("messages")
      .withIndex("by_thread_t", (q) => q.eq("threadId", threadId))
      .order("asc").take(args.limit ?? 200);
  },
});

export const listInternal = internalQuery({
  args: { threadId: v.string(), limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    return await ctx.db.query("messages")
      .withIndex("by_thread_t", (q) => q.eq("threadId", args.threadId))
      .order("asc").take(args.limit ?? 60);
  },
});

/** User sends a message. Schedules the agent action to reply. */
export const send = mutation({
  args: { threadId: v.optional(v.string()), text: v.string() },
  handler: async (ctx, args) => {
    const threadId = args.threadId ?? DEFAULT_THREAD;
    await ctx.db.insert("messages", {
      threadId, role: "user", text: args.text, createdAt: Date.now(),
    });
    await ctx.scheduler.runAfter(0, internal.agent.chat, { threadId, userMessage: args.text });
  },
});

export const appendInternal = internalMutation({
  args: {
    threadId: v.string(),
    role: v.union(v.literal("user"), v.literal("assistant"), v.literal("tool")),
    text: v.string(),
    tool: v.optional(v.object({ name: v.string(), args: v.any() })),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("messages", { ...args, createdAt: Date.now() });
  },
});
