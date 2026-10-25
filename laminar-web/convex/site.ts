/** Small public snapshot for the website hosted in Mana. Read-only; no names beyond who approved a run. */
import { internalQuery, query, type QueryCtx } from "./_generated/server";
import { stats } from "./qc";

export const state = internalQuery({
  args: {},
  handler: async (ctx) => {
    const tubes = (await ctx.db.query("tubes").collect()).sort((a, b) => a.slot - b.slot);
    const wells = (await ctx.db.query("wells").collect()).sort((a, b) => a.number - b.number);
    const runs = await ctx.db.query("runs").withIndex("by_createdAt").order("desc").take(60);
    const pours = await ctx.db.query("pours").withIndex("by_t").order("desc").take(60);
    const running = runs.find((r) => r.status === "running");
    const refusals = runs.filter((r) => r.status === "rejected" && r.errors?.length);
    // Prefer the caught tube swap, the case the site tells the story about.
    const lastRefusal = refusals.find((r) => r.errors!.some((e) => e.includes("rack"))) ?? refusals[0];
    const last = runs.find((r) => r.status === "done");
    const step = running?.step ?? 0;
    const inHand = running?.moves?.slice(0, step).reverse().find((m) => m.startsWith("grab_") || m.startsWith("release_"));
    return {
      t: Date.now(),
      tubes: tubes.map((t) => ({ slot: t.slot, letter: t.letter, volumeMl: t.volumeMl, fillLineMl: t.fillLineMl })),
      wells: wells.map((w) => ({ number: w.number, volumeMl: w.volumeMl, capacityMl: w.capacityMl, contents: w.contents })),
      running: running ? {
        request: running.request, step, total: running.moves?.length ?? 0,
        move: running.moves?.[Math.max(0, step - 1)] ?? null,
        holding: inHand?.startsWith("grab_") ? inHand.slice(5) : null,
      } : null,
      lastResult: last ? { request: last.request, result: last.result ?? null, summary: last.summary ?? null, finishedAt: last.finishedAt ?? null } : null,
      lastRefusal: lastRefusal ? { request: lastRefusal.request, error: lastRefusal.errors!.find((e) => e.includes("rack")) ?? lastRefusal.errors![0] } : null,
      runs: { total: runs.length, done: runs.filter((r) => r.status === "done").length, refused: runs.filter((r) => r.status === "rejected").length },
      pours: pours.reverse().map((p) => ({ tube: p.tube, targetMl: p.targetMl, pouredMl: p.pouredMl, errorMl: p.errorMl, source: p.source, t: p.t })),
      qc: stats(pours),
      totals: { pours: (await ctx.db.query("pours").collect()).length, runs: (await ctx.db.query("runs").collect()).filter((r) => r.status === "done").length },
    };
  },
});

/** What the landing page's arm needs: the latest joint samples and what the current run is doing. */
export async function armSnapshot(ctx: QueryCtx) {
  const samples = (await ctx.db.query("telemetry").withIndex("by_t").order("desc").take(12)).reverse();
  const run = await ctx.db.query("runs").withIndex("by_status", (q) => q.eq("status", "running")).first();
  const move = run?.moves?.[Math.max(0, (run.step ?? 1) - 1)] ?? null;
  // The latest pour of the running run: target, measured volume and when it started (server ms).
  let pour: { targetMl: number; pouredMl: number; t: number; well: number } | null = null;
  if (run) {
    const last = await ctx.db.query("pours").withIndex("by_runId", (q) => q.eq("runId", run._id)).order("desc").first();
    if (last) pour = { targetMl: last.targetMl, pouredMl: last.pouredMl, t: last.t, well: last.well };
  }
  return {
    samples: samples.map((s) => ({ t: s.t, joints: s.joints, gripperDeg: s.gripperDeg ?? null, source: s.source, move: s.move ?? null })),
    run: run ? { request: run.request, step: run.step ?? 0, total: run.moves?.length ?? 0, move } : null,
    pour,
  };
}

/** HTTP form of the snapshot (http.ts adds the server time). */
export const arm = internalQuery({ args: {}, handler: (ctx) => armSnapshot(ctx) });

/** Live query for the landing page's 3D arm: Convex pushes a new result whenever telemetry, runs or pours change. */
export const liveArm = query({ args: {}, handler: (ctx) => armSnapshot(ctx) });

/** Live query for the landing page's console band. */
export const liveStats = query({
  args: {},
  handler: async (ctx) => {
    const runs = await ctx.db.query("runs").collect();
    const pours = await ctx.db.query("pours").collect();
    const running = runs.find((r) => r.status === "running");
    const done = runs.filter((r) => r.status === "done");
    const last = done.reduce<(typeof done)[number] | null>((a, r) => (!a || (r.finishedAt ?? 0) > (a.finishedAt ?? 0) ? r : a), null);
    return {
      running: running ? { request: running.request, step: running.step ?? 0, total: running.moves?.length ?? 0 } : null,
      pours: pours.length, runsDone: done.length, refused: runs.filter((r) => r.status === "rejected").length,
      lastFinishedAt: last?.finishedAt ?? null,
    };
  },
});
