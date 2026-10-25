/**
 * Bridge for the Laminar Lab Console app in Mana. That app polls GET /mana/summary every 5 s and sends
 * POST /mana/submit (see http.ts). Its model is a 12-channel reservoir with blue/red liquids, so wells 1–4
 * are reported as channels 1, 4, 7, 10, and tubes A–D as the liquids red, blue, yellow and green.
 */
import { v } from "convex/values";
import { internalMutation, internalQuery, query } from "./_generated/server";
import { EXECUTOR, createRun } from "./runs";

const WELL_CHANNEL: Record<number, number> = { 1: 1, 2: 4, 3: 7, 4: 10 };
const LIQUID: Record<string, string> = { A: "red", B: "blue", C: "yellow", D: "green" };
const COLOR: Record<string, string> = { A: "red (sample A)", B: "blue (reagent B)", C: "yellow (reagent C)", D: "green (reagent D)" };
const WINDOW_MS = 48 * 3600 * 1000;

const STATUS: Record<string, string> = {
  checking: "planning", rejected: "failed", awaiting_approval: "awaiting_approval",
  approved: "approved", running: "running", done: "done", aborted: "aborted",
};

export const summary = internalQuery({
  args: {},
  handler: async (ctx) => {
    const to = Date.now(), from = to - WINDOW_MS;
    const runs = await ctx.db.query("runs").withIndex("by_createdAt", (q) => q.gte("createdAt", from)).order("desc").take(2000);
    const pours = await ctx.db.query("pours").withIndex("by_t", (q) => q.gte("t", from)).take(20000);
    const wells = await ctx.db.query("wells").collect();
    const jobs = runs.map((r) => {
      const ps = pours.filter((p) => p.runId === r._id);
      const tubes = [...new Set((r.tasks ?? []).map((t) => t.tube))];
      return {
        id: r._id, request: r.request, status: STATUS[r.status] ?? r.status,
        targetMl: (r.tasks ?? []).reduce((s, t) => s + t.pours.reduce((a, p) => a + p.ml, 0), 0) || null,
        channel: null, approvedBy: r.approvedBy ?? null,
        failReason: r.abortReason ?? (r.errors?.length ? r.errors.join(" ") : r.question ?? null),
        createdAt: r.createdAt, finishedAt: r.finishedAt ?? null,
        liquid: tubes.length === 1 ? LIQUID[tubes[0]] : null,
        pours: ps.length,
      };
    });
    const channels = Array.from({ length: 12 }, (_, i) => {
      const number = i + 1;
      const well = wells.find((w) => WELL_CHANNEL[w.number] === number);
      if (!well) return { number, contents: "splash channel", currentMl: 0, targetMl: null, capacityMl: 3 };
      return {
        number,
        contents: `well ${well.number}${well.number === 4 ? " (control)" : ""}: ` + (well.contents.map((c) => `${c.ml.toFixed(2)} mL ${COLOR[c.tube]}`).join(", ") || "empty"),
        currentMl: well.volumeMl, targetMl: null, capacityMl: well.capacityMl,
      };
    });
    return {
      jobs,
      pours: pours.map((p) => ({
        jobId: p.runId, targetMl: p.targetMl, pouredMl: p.pouredMl, errorMl: p.errorMl,
        createdAt: p.t, liquid: LIQUID[p.tube], tube: p.tube, well: p.well, channel: WELL_CHANNEL[p.well], source: p.source,
      })),
      channels,
      coverage: { from, to, complete: pours.length < 20000 && runs.length < 2000 },
    };
  },
});

/** Called on each summary poll; written at most every 15 s. */
export const seen = internalMutation({
  args: {},
  handler: async (ctx) => {
    const row = await ctx.db.query("integrations").withIndex("by_name", (q) => q.eq("name", "mana")).first();
    const now = Date.now();
    if (!row) await ctx.db.insert("integrations", { name: "mana", lastSeen: now, polls: 1 });
    else if (now - row.lastSeen > 15_000) await ctx.db.patch(row._id, { lastSeen: now, polls: row.polls + 1 });
  },
});

export const status = query({
  args: {},
  handler: async (ctx) => {
    const row = await ctx.db.query("integrations").withIndex("by_name", (q) => q.eq("name", "mana")).first();
    const runs = await ctx.db.query("runs").withIndex("by_createdAt").order("desc").take(200);
    return { lastSeen: row?.lastSeen ?? null, runsFromMana: runs.filter((r) => r.via === "mana").length };
  },
});

export const submit = internalMutation({
  args: { requestText: v.string(), requestedBy: v.string() },
  handler: async (ctx, a) => {
    // The Mana console names wells by reservoir channel (1, 4, 7, 10). Translate in code, so the
    // planner never has to guess what "channel 4" means. Splash channels pass through unchanged
    // and the planner asks about them.
    const text = a.requestText.replace(/\bchannel\s+(\d{1,2})\b/gi, (m, c) => {
      const well = Object.entries(WELL_CHANNEL).find(([, ch]) => ch === +c)?.[0];
      return well ? `well ${well}` : m;
    });
    const id = await createRun(ctx, text, a.requestedBy || "Mana console", EXECUTOR, "mana", true);
    return { jobId: id, status: "planning" };
  },
});
