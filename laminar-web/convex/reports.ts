import { v } from "convex/values";
import { internalMutation, mutation, query, type MutationCtx } from "./_generated/server";
import { log } from "./runs";
import { stats } from "./qc";
import { LOW_TUBE_ML } from "./lib";

const TZ = "America/Los_Angeles";
const DAY = 24 * 3600 * 1000;

function laParts(t: number) {
  const p = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", hourCycle: "h23" })
    .formatToParts(new Date(t));
  const g = (k: string) => p.find((x) => x.type === k)!.value;
  return { date: `${g("year")}-${g("month")}-${g("day")}`, hour: parseInt(g("hour")) };
}

async function generate(ctx: MutationCtx, trigger: "schedule" | "manual") {
  const to = Date.now(), from = to - DAY;
  const runs = (await ctx.db.query("runs").withIndex("by_createdAt").order("desc").take(500))
    .filter((r) => (r.finishedAt ?? r.createdAt) >= from);
  const pours = await ctx.db.query("pours").withIndex("by_t", (q) => q.gte("t", from)).collect();
  const s = stats(pours);
  const perTube = (["A", "B", "C", "D"] as const).flatMap((tube) => {
    const st = stats(pours.filter((p) => p.tube === tube));
    return st.n ? [{ tube, pours: st.n, meanErrorMl: st.meanErrorMl!, maeMl: st.maeMl! }] : [];
  });
  const tubes = await ctx.db.query("tubes").collect();
  const lowTubes = tubes.filter((t) => t.volumeMl < LOW_TUBE_ML).map((t) => `${t.letter} (${t.volumeMl.toFixed(1)} mL)`);
  const aborted = runs.filter((r) => r.status === "aborted");

  // One advisory recommendation, rule-based. It never changes settings or moves the arm.
  let recommendation = "No pours in the last 24 hours. Run one standard assay with water to confirm the bench before real samples.";
  if (s.n) {
    const worst = [...perTube].sort((a, b) => Math.abs(b.meanErrorMl) - Math.abs(a.meanErrorMl))[0];
    if (lowTubes.length) recommendation = `Refill ${lowTubes.join(", ")} to the fill line before the first run; low tubes start flowing at a different tilt and pour less accurately.`;
    else if (s.inBandPct! < 90 && worst) recommendation = `Only ${s.inBandPct}% of pours landed within ±10%. Tube ${worst.tube} is furthest off (mean ${worst.meanErrorMl > 0 ? "+" : ""}${worst.meanErrorMl.toFixed(2)} mL); re-record its stop-early offset with water.`;
    else if (Math.abs(s.meanErrorMl!) > 0.02) recommendation = `Pours are consistently ${s.meanErrorMl! < 0 ? "short" : "over"} by ${Math.abs(s.meanErrorMl!).toFixed(2)} mL on average. Adjust the stop-early offset by that amount.`;
    else recommendation = `Pours are on target (${s.inBandPct}% within ±10%, mean ${s.meanErrorMl! >= 0 ? "+" : "−"}${Math.abs(s.meanErrorMl!).toFixed(3)} mL). No change needed.`;
  }

  const id = await ctx.db.insert("reports", {
    date: laParts(to).date, generatedAt: to, trigger, windowFrom: from, windowTo: to,
    runs: runs.length, done: runs.filter((r) => r.status === "done").length, aborted: aborted.length,
    abortReasons: aborted.map((r) => r.abortReason ?? "unspecified"),
    pours: s.n, meanErrorMl: s.meanErrorMl, maeMl: s.maeMl, inBandPct: s.inBandPct,
    perTube, lowTubes, recommendation,
  });
  await log(ctx, trigger === "schedule" ? "cron" : "operator", "report", `QC report for ${laParts(to).date}: ${s.n} pours. ${recommendation}`);
  return id;
}

/** Hourly cron; generates once when it is 08:00 in Los Angeles, so daylight saving is handled by the timezone. */
export const tick = internalMutation({
  args: {},
  handler: async (ctx) => {
    const { date, hour } = laParts(Date.now());
    if (hour !== 8) return;
    const existing = await ctx.db.query("reports").withIndex("by_date", (q) => q.eq("date", date)).collect();
    if (existing.some((r) => r.trigger === "schedule")) return;
    await generate(ctx, "schedule");
  },
});

export const generateNow = mutation({ args: {}, handler: async (ctx) => generate(ctx, "manual") });

export const list = query({
  args: {},
  handler: async (ctx) => ctx.db.query("reports").order("desc").take(30),
});

export const get = query({
  args: { id: v.id("reports") },
  handler: async (ctx, { id }) => ctx.db.get(id),
});
