import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import { BAND, r2 } from "./lib";

const r3 = (x: number) => Math.round(x * 1000) / 1000;
import type { Doc } from "./_generated/dataModel";

export function stats(pours: Doc<"pours">[]) {
  if (pours.length === 0) return { n: 0, meanErrorMl: null, maeMl: null, sdMl: null, inBandPct: null };
  const errs = pours.map((p) => p.errorMl);
  const mean = errs.reduce((s, e) => s + e, 0) / errs.length;
  const mae = errs.reduce((s, e) => s + Math.abs(e), 0) / errs.length;
  const sd = Math.sqrt(errs.reduce((s, e) => s + (e - mean) ** 2, 0) / errs.length);
  const inBand = pours.filter((p) => Math.abs(p.errorMl) <= BAND * p.targetMl).length;
  return { n: pours.length, meanErrorMl: r3(mean), maeMl: r3(mae), sdMl: r3(sd), inBandPct: Math.round((inBand / pours.length) * 100) };
}

/** Recent pours with the summary the QC tab shows. Live: updates as each pour lands. */
export const recent = query({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, { limit }) => {
    const pours = await ctx.db.query("pours").withIndex("by_t").order("desc").take(Math.min(limit ?? 80, 400));
    const runs = await ctx.db.query("runs").withIndex("by_createdAt").order("desc").take(200);
    const terminal = runs.filter((r) => r.status === "done" || r.status === "aborted");
    return {
      pours: pours.reverse(),
      overall: stats(pours),
      perTube: (["A", "B", "C", "D"] as const).map((tube) => ({ tube, ...stats(pours.filter((p) => p.tube === tube)) })),
      runSuccessPct: terminal.length ? Math.round((terminal.filter((r) => r.status === "done").length / terminal.length) * 100) : null,
      aborts: runs.filter((r) => r.status === "aborted").slice(0, 6).map((r) => ({ t: r.finishedAt ?? r.createdAt, reason: r.abortReason ?? "" })),
      refusals: runs.filter((r) => r.status === "rejected" && r.errors?.length).length,
    };
  },
});

// ---------------------------------------------------------------- Beat the Robot

export const leaderboard = query({
  args: {},
  handler: async (ctx) => {
    const scores = await ctx.db.query("scores").withIndex("by_t").order("desc").take(500);
    const best = new Map<string, Doc<"scores"> & { rounds: number }>();
    for (const s of scores) {
      const k = s.player.toLowerCase();
      const cur = best.get(k);
      if (!cur) best.set(k, { ...s, rounds: 1 });
      else { cur.rounds++; if (Math.abs(s.errorMl) < Math.abs(cur.errorMl)) Object.assign(cur, { ...s, rounds: cur.rounds }); }
    }
    const bench = await ctx.db.query("pours").withIndex("by_source_t", (q) => q.eq("source", "bench")).order("desc").take(200);
    const sim = await ctx.db.query("pours").withIndex("by_source_t", (q) => q.eq("source", "sim")).order("desc").take(200);
    const mae = (ps: Doc<"pours">[]) => (ps.length ? r3(ps.reduce((s, p) => s + Math.abs(p.errorMl), 0) / ps.length) : null);
    return {
      humans: [...best.values()].sort((a, b) => Math.abs(a.errorMl) - Math.abs(b.errorMl)).slice(0, 12),
      robot: { benchMae: mae(bench), benchN: bench.length, simMae: mae(sim), simN: sim.length },
    };
  },
});

export const submitScore = mutation({
  args: { player: v.string(), targetMl: v.number(), pouredMl: v.number() },
  handler: async (ctx, a) => {
    const player = a.player.trim().slice(0, 24);
    if (!player) throw new Error("Enter a name for the leaderboard.");
    if (!(a.targetMl >= 1 && a.targetMl <= 4) || !(a.pouredMl >= 0 && a.pouredMl < 20)) throw new Error("Score out of range.");
    await ctx.db.insert("scores", { player, targetMl: r2(a.targetMl), pouredMl: r2(a.pouredMl), errorMl: r2(a.pouredMl - a.targetMl), t: Date.now() });
  },
});
