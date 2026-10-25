import { v } from "convex/values";
import { internalMutation, internalQuery, mutation, query } from "./_generated/server";
import { internal } from "./_generated/api";
import { approveRun, cancelRun } from "./runs";
import { limiter } from "./limits";

/** The assistant chat's storage. Claude's replies are written by agent.ts; approve / cancel / stop are decided here,
 *  by a named person, never by the model. */

const VIA = v.optional(v.union(v.literal("chat"), v.literal("voice")));

export const list = query({
  args: { threadId: v.string() },
  handler: async (ctx, { threadId }) =>
    (await ctx.db.query("messages").withIndex("by_thread_t", (q) => q.eq("threadId", threadId)).order("desc").take(200)).reverse(),
});

/** The conversation's plan that is waiting, queued or running, if any: what a typed "approve" or "stop" acts on. */
export const active = query({
  args: { threadId: v.string() },
  handler: async (ctx, { threadId }) => {
    for (const status of ["running", "approved", "awaiting_approval"] as const) {
      const run = await ctx.db.query("runs").withIndex("by_status", (q) => q.eq("status", status))
        .filter((q) => q.eq(q.field("threadId"), threadId)).order("desc").first();
      if (run) return run;
    }
    return null;
  },
});

/** The thread's latest messages, oldest first, with each plan card's run as it is now. */
export const listInternal = internalQuery({
  args: { threadId: v.string(), limit: v.number() },
  handler: async (ctx, { threadId, limit }) => {
    const rows = (await ctx.db.query("messages").withIndex("by_thread_t", (q) => q.eq("threadId", threadId)).order("desc").take(limit)).reverse();
    return Promise.all(rows.map(async (m) => ({ ...m, run: m.runId ? await ctx.db.get(m.runId) : null })));
  },
});

export const append = internalMutation({
  args: { threadId: v.string(), text: v.string(), runId: v.optional(v.id("runs")) },
  handler: async (ctx, a) => { await ctx.db.insert("messages", { ...a, role: "assistant", t: Date.now() }); },
});

function who(by: string) {
  const n = by.trim();
  if (n.length < 2) throw new Error("Enter your name at the top first, so the log records who asked.");
  return n.slice(0, 40);
}

/** A typed or spoken request: stored, then the assistant answers in the background. */
export const send = mutation({
  args: { threadId: v.string(), text: v.string(), by: v.string(), via: VIA },
  handler: async (ctx, a) => {
    const by = who(a.by);
    const text = a.text.trim().slice(0, 1000);
    if (!text) return;
    await limiter.limit(ctx, "chat", { key: by.toLowerCase(), throws: true });
    await ctx.db.insert("messages", { threadId: a.threadId, role: "user", text, by, via: a.via ?? "chat", t: Date.now() });
    await ctx.scheduler.runAfter(0, internal.agent.chat, { threadId: a.threadId, by });
  },
});

/** Approve, cancel or stop from the chat: the person's words go in the thread, the decision goes through the same
 *  code as the Run tab's buttons (re-checked at approval, logged under their name). */
export const decide = mutation({
  args: { threadId: v.string(), runId: v.id("runs"), decision: v.union(v.literal("approve"), v.literal("cancel"), v.literal("stop")), by: v.string(), via: VIA, said: v.optional(v.string()) },
  handler: async (ctx, a) => {
    const by = who(a.by);
    const via = a.via ?? "chat";
    const tag = via === "voice" ? `${by} (voice)` : `${by} (chat)`;
    const said = a.said?.trim() || { approve: "Approve", cancel: "Cancel", stop: "Stop" }[a.decision];
    await ctx.db.insert("messages", { threadId: a.threadId, role: "user", text: said, by, via, t: Date.now() });
    const reply = async (text: string) => ctx.db.insert("messages", { threadId: a.threadId, role: "assistant", text, t: Date.now() });
    const run = await ctx.db.get(a.runId);
    if (!run) return reply("That plan no longer exists.");
    if (a.decision === "approve") {
      if (run.status !== "awaiting_approval") return reply(`That plan is ${run.status.replace("_", " ")}, so there is nothing to approve.`);
      const r = await approveRun(ctx, a.runId, tag);
      return reply(r.ok ? `Approved by ${by}. Starting now.` : `The bench changed since the plan was checked, so it was refused: ${r.errors.join(" ")}`);
    }
    if (a.decision === "cancel") {
      if (run.status !== "awaiting_approval") return reply(`That plan is ${run.status.replace("_", " ")}; only a plan waiting for approval can be cancelled.`);
      await cancelRun(ctx, a.runId, tag, `Cancelled by ${via} before motion`);
      return reply("Cancelled. Nothing moved.");
    }
    if (!["running", "approved", "awaiting_approval"].includes(run.status)) return reply("Nothing is running.");
    await cancelRun(ctx, a.runId, tag, `Stopped by ${via}`);
    return reply("Stopped. Nothing else will move.");
  },
});
