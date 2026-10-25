import { mutation, query, internalQuery } from "./_generated/server";
import type { QueryCtx } from "./_generated/server";
import { v } from "convex/values";
import { internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import { readiness } from "./station";

/** Requests from other labs' agents (Fetch.ai uAgents, ASI:One: see fetch/ in the repo).
 *
 *  Each remote sender gets its own chat thread, "remote:<agent address>", with the same assistant, tools and checks
 *  as the Lab page: the request is planned, checked against the bench and put in front of a PERSON here (the Lab
 *  page shows it as a permission card from that lab). The bench agent reads the replies and the run's progress back
 *  out of the thread and sends them to the requester.
 *
 *  Only on a SIMULATED bench may the requester approve its own plan (so anyone can try the whole loop from ASI:One);
 *  on the real arm only the technician at the bench can. The requester can always cancel its own waiting plan. */

const PREFIX = "remote:";
const threadOf = (sender: string) => PREFIX + sender;

async function remoteJob(ctx: QueryCtx, sender: string, jobId: string): Promise<Doc<"jobs">> {
  const job = await ctx.db.get(jobId as Id<"jobs">).catch(() => null);
  if (!job || job.threadId !== threadOf(sender)) throw new Error("no plan of yours with that id");
  return job;
}

/** A request (or any chat message) from another lab's agent. The assistant answers in the sender's thread. */
export const ask = mutation({
  args: { sender: v.string(), text: v.string(), label: v.optional(v.string()), via: v.optional(v.string()),
          newSession: v.optional(v.boolean()) },   // a new conversation (e.g. a new ASI:One chat): start fresh
  handler: async (ctx, a) => {
    const threadId = threadOf(a.sender);
    const now = Date.now();
    const r = await ctx.db.query("remotes").withIndex("by_thread", (q) => q.eq("threadId", threadId)).first();
    const label = a.label?.trim() || r?.label || `agent ${a.sender.slice(0, 16)}…`;
    const session = a.newSession || !r ? { sessionStart: now } : {};
    if (r) await ctx.db.patch(r._id, { label, via: a.via ?? r.via, lastSeenAt: now, ...session });
    else await ctx.db.insert("remotes", { threadId, sender: a.sender, label, via: a.via ?? "agent", createdAt: now,
                                          lastSeenAt: now, ...session });
    await ctx.db.insert("messages", { threadId, role: "user", text: a.text, createdAt: now });
    await ctx.scheduler.runAfter(0, internal.agent.chat, { threadId, userMessage: a.text });
    return { threadId, at: now };
  },
});

/** What the assistant (and the bench) said in the sender's thread after `since` (ms): replies, plan cards,
 *  approvals and run summaries, oldest first. */
export const replies = query({
  args: { sender: v.string(), since: v.number() },
  handler: async (ctx, a) => {
    const rows = await ctx.db.query("messages")
      .withIndex("by_thread_t", (q) => q.eq("threadId", threadOf(a.sender)).gt("createdAt", a.since))
      .order("asc").take(50);
    return rows.filter((m) => m.role !== "user")
      .map((m) => ({ at: m.createdAt, role: m.role, text: m.text, tool: m.tool?.name ?? null, args: m.tool?.args ?? null }));
  },
});

/** The sender's plans, newest first, with where each one is and, once finished, what was measured. */
export const runs = query({
  args: { sender: v.string(), limit: v.optional(v.number()) },
  handler: async (ctx, a) => {
    const jobs = await ctx.db.query("jobs").withIndex("by_createdAt").order("desc").take(300);
    type S = { skill: string; status: string; reason?: string; telemetry?: Record<string, unknown> };
    return jobs.filter((j) => j.threadId === threadOf(a.sender)).slice(0, a.limit ?? 5).map((j) => ({
      plan_id: j._id, request: j.requestText, status: j.status, created: j.createdAt, finished: j.finishedAt ?? null,
      approved_by: j.approvedBy ?? null, fail_reason: j.failReason ?? null, summary: j.summary ?? null,
      pours: j.pours ?? [], steps: (j.plan ?? []).map((s) => s.label ?? s.name),
      // one camera check per pour, in order
      results: ((j.report?.steps ?? []) as S[]).filter((s) => s.telemetry?.measured_ml != null).map((s, i) => ({
        container: j.pours?.[i]?.container ?? null, target_ml: j.pours?.[i]?.ml ?? null,
        measured_ml: s.telemetry?.measured_ml, error_ml: s.telemetry?.error_ml ?? null, ok: s.status === "ok" })),
    }));
  },
});

/** Is the bench simulated (then the requester may approve its own plan), and is it ready? */
export const bench = query({
  args: {},
  handler: async (ctx) => {
    const st = await ctx.db.query("benchStatus").first();
    const online = !!st && Date.now() - st.t < 15000;
    return { online, simulated: !!st?.simulated && online, not_ready: await readiness(ctx), busy: st?.busy ?? null };
  },
});

/** The requester approves its own plan: ONLY on a simulated bench. On the real arm a person here approves. */
export const approve = mutation({
  args: { sender: v.string(), jobId: v.string() },
  handler: async (ctx, a) => {
    const job = await remoteJob(ctx, a.sender, a.jobId);
    if (job.status !== "awaiting_approval") throw new Error(`that plan is ${job.status}, not waiting for approval`);
    const st = await ctx.db.query("benchStatus").first();
    if (!st?.simulated || Date.now() - st.t > 15000) {
      throw new Error("this bench runs the real arm: only the technician at the bench can approve. They have your request.");
    }
    const notReady = await readiness(ctx);
    if (notReady) throw new Error(`can't run it: ${notReady}`);
    const remote = await ctx.db.query("remotes").withIndex("by_thread", (q) => q.eq("threadId", job.threadId!)).first();
    const who = `${remote?.label ?? "requester"} (simulated bench)`;
    await ctx.db.patch(job._id, { status: "approved", approvedBy: who, approvedVia: remote?.via ?? "agent" });
    await ctx.db.insert("events", { t: Date.now(), kind: "info", source: "remote", jobId: job._id,
                                    message: `plan approved by ${who} on the simulated bench` });
    await ctx.db.insert("messages", {
      threadId: job.threadId!, role: "tool", createdAt: Date.now(),
      text: `Approved by ${who}. Sent to the simulated robot.`,
      tool: { name: "approval", args: { jobId: job._id, approved: true, by: who } },
    });
    return { ok: true };
  },
});

/** The requester withdraws its own waiting plan. */
export const cancel = mutation({
  args: { sender: v.string(), jobId: v.string() },
  handler: async (ctx, a) => {
    const job = await remoteJob(ctx, a.sender, a.jobId);
    if (job.status !== "awaiting_approval") throw new Error(`that plan is ${job.status}: only a waiting plan can be cancelled`);
    await ctx.db.patch(job._id, { status: "rejected", finishedAt: Date.now(), failReason: "cancelled by the requester" });
    await ctx.db.insert("messages", {
      threadId: job.threadId!, role: "tool", createdAt: Date.now(), text: "Cancelled by the requester. Nothing moved.",
      tool: { name: "approval", args: { jobId: job._id, approved: false, by: "requester" } },
    });
    return { ok: true };
  },
});

/** For the Lab page: other labs' plans waiting for a person here, or running, with their permission card. */
export const inbox = query({
  args: {},
  handler: async (ctx) => {
    const jobs = await ctx.db.query("jobs").withIndex("by_createdAt").order("desc").take(100);
    const open = jobs.filter((j) => j.threadId?.startsWith(PREFIX) &&
      ["awaiting_approval", "approved", "running", "pouring"].includes(j.status));
    const out = [];
    for (const j of open) {
      const msgs = await ctx.db.query("messages")
        .withIndex("by_thread_t", (q) => q.eq("threadId", j.threadId!)).order("desc").take(30);
      const card = msgs.find((m) => m.tool?.name === "plan" && (m.tool.args as { jobId?: string })?.jobId === j._id);
      if (card) out.push({ jobId: j._id, from: j.requestedBy ?? "another lab", via: j.requestedVia ?? "agent",
                           request: j.requestText, args: card.tool!.args });
    }
    return out;
  },
});

export const getInternal = internalQuery({
  args: { threadId: v.string() },
  handler: async (ctx, a) => await ctx.db.query("remotes").withIndex("by_thread", (q) => q.eq("threadId", a.threadId)).first(),
});
