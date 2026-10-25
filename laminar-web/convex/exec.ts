/**
 * Simulated bench executor as a durable Convex Workflow: one step per move. A run survives deploys and
 * restarts, a Stop cancels the workflow, each move writes the arm's target pose to `telemetry` (for the
 * 3D arm) and pours update tubes, wells and Tiger Data the way the real bench would.
 */
import { v } from "convex/values";
import { WorkflowManager, vWorkflowId } from "@convex-dev/workflow";
import { vResultValidator } from "@convex-dev/workpool";
import { components, internal } from "./_generated/api";
import { internalMutation, internalQuery } from "./_generated/server";
import type { Id } from "./_generated/dataModel";
import { log } from "./runs";
import { BAND, CONTROL_WELL, r2 } from "./lib";
import { HOME, GRIPPER_OPEN, poseFor, spotAfter } from "./poses";

export const workflow = new WorkflowManager(components.workflow, {
  workpoolOptions: { maxParallelism: 4 },
});

export const runAssay = workflow
  .define({ args: { runId: v.id("runs") } })
  .handler(async (step, { runId }): Promise<void> => {
    const total = await step.runQuery(internal.exec.moveCount, { runId });
    let wait = 300;
    for (let i = 0; i < total; i++) {
      const r = await step.runMutation(internal.exec.doMove, { runId, i }, { runAfter: wait, name: `move ${i + 1}` });
      if (r.stop) return;
      wait = r.ms;
    }
    await step.runMutation(internal.exec.finishRun, { runId }, { runAfter: wait });
    // Claude explains the readout; retried with backoff if the API hiccups.
    await step.runAction(internal.planner.reflect, { id: runId }, { retry: true });
  });

/** If the workflow itself fails, the run must not stay "running" forever. */
export const onWorkflowDone = internalMutation({
  args: { workflowId: vWorkflowId, result: vResultValidator, context: v.object({ runId: v.id("runs") }) },
  handler: async (ctx, { result, context }) => {
    const run = await ctx.db.get(context.runId);
    if (run?.status === "running" && result.kind !== "success") {
      const why = result.kind === "failed" ? `Executor failed: ${result.error}` : "Executor cancelled";
      await ctx.db.patch(run._id, { status: "aborted", abortReason: why, finishedAt: Date.now() });
      await log(ctx, "executor", "aborted", why, run._id);
    }
    await ctx.scheduler.runAfter(0, internal.runs.startNext, {});
  },
});

export const moveCount = internalQuery({
  args: { runId: v.id("runs") },
  handler: async (ctx, { runId }) => (await ctx.db.get(runId))?.moves?.length ?? 0,
});

// Gaussian noise for the simulated pour controller (Box–Muller).
const gauss = () => Math.sqrt(-2 * Math.log(1 - Math.random())) * Math.cos(2 * Math.PI * Math.random());

export const doMove = internalMutation({
  args: { runId: v.id("runs"), i: v.number() },
  handler: async (ctx, { runId, i }): Promise<{ stop: boolean; ms: number }> => {
    const run = await ctx.db.get(runId);
    if (!run || run.status !== "running" || !run.moves) return { stop: true, ms: 0 };
    const move = run.moves[i];
    const now = Date.now();

    // Arm pose: where it is (last sample), where this move takes it.
    const last = await ctx.db.query("telemetry").withIndex("by_t").order("desc").first();
    let spot = HOME;
    for (const m of run.moves.slice(0, i)) spot = spotAfter(m, spot);
    const pose = poseFor(move, { spot });
    const from = last?.joints ?? HOME;
    // Two samples: the pose now, and the target pose when the move ends. The 3D arm interpolates between them.
    await ctx.db.insert("telemetry", { t: now, joints: from, gripperDeg: last?.gripperDeg ?? GRIPPER_OPEN, source: "sim", runId, move });
    await ctx.db.insert("telemetry", { t: now + pose.ms, joints: pose.joints, gripperDeg: pose.gripper, source: "sim", runId, move });

    const m = move.match(/^pour\(well(\d), ([\d.]+) mL\)$/);
    if (m) {
      const well = parseInt(m[1]), target = parseFloat(m[2]);
      const tube = run.moves.slice(0, i).reverse().find((x) => x.startsWith("grab_"))!.slice(5) as "A" | "B" | "C" | "D";
      const tubeRow = await ctx.db.query("tubes").filter((q) => q.eq(q.field("letter"), tube)).first();
      // controller error: ~3% sd, slight under-pour bias; worse when the tube is running low
      const low = tubeRow && tubeRow.volumeMl < 1.5 ? 1.8 : 1;
      const poured = Math.max(0, r2(target * (1 - 0.01 + 0.03 * low * gauss())));
      const err = r2(poured - target);
      await ctx.db.insert("pours", { runId, tube, well, targetMl: target, pouredMl: poured, errorMl: err, source: "sim", t: now });
      if (tubeRow) await ctx.db.patch(tubeRow._id, { volumeMl: r2(Math.max(0, tubeRow.volumeMl - poured)), updatedAt: now });
      const w = await ctx.db.query("wells").withIndex("by_number", (q) => q.eq("number", well)).first();
      if (w) {
        const contents = [...w.contents];
        const c = contents.find((x) => x.tube === tube);
        if (c) c.ml = r2(c.ml + poured); else contents.push({ tube, ml: poured });
        await ctx.db.patch(w._id, { volumeMl: r2(w.volumeMl + poured), contents, updatedAt: now });
      }
      await ctx.scheduler.runAfter(0, internal.tiger.ingestPour, { t: now, runId, tube, well, targetMl: target, pouredMl: poured, errorMl: err, source: "sim" });
      if (Math.abs(err) > BAND * target) await log(ctx, "executor", "warn", `Pour into well ${well} added ${poured.toFixed(2)} mL instead of ${target.toFixed(2)} mL.`, runId);
    }
    await ctx.db.patch(runId, { step: i + 1 });
    return { stop: false, ms: pose.ms };
  },
});

export const finishRun = internalMutation({
  args: { runId: v.id("runs") },
  handler: async (ctx, { runId }) => {
    const run = await ctx.db.get(runId);
    if (!run || run.status !== "running") return;
    const pours = await ctx.db.query("pours").withIndex("by_runId", (q) => q.eq("runId", runId)).collect();
    const wells = await ctx.db.query("wells").collect();
    const control = wells.find((w) => w.number === CONTROL_WELL);
    const reacted = wells.filter((w) => w.number !== CONTROL_WELL && w.contents.some((c) => c.tube !== "A")).map((w) => w.number);
    const off = pours.filter((p) => Math.abs(p.errorMl) > BAND * p.targetMl);
    const result = [
      reacted.length ? `${reacted.length > 1 ? "Wells" : "Well"} ${reacted.join(", ")} changed color against the control (well ${CONTROL_WELL}, ${control?.volumeMl.toFixed(2) ?? "0"} mL of sample only).` : "No well changed color against the control.",
      `${pours.length} pours, ${pours.length - off.length} within ±10% of target.`,
      off.length ? `Check ${off.map((p) => `well ${p.well} (${p.pouredMl.toFixed(2)} of ${p.targetMl.toFixed(2)} mL)`).join(", ")}.` : "",
    ].filter(Boolean).join(" ");
    await ctx.db.patch(runId, { status: "done", finishedAt: Date.now(), result });
    // arm goes home
    const t = Date.now();
    await ctx.db.insert("telemetry", { t: t + 900, joints: HOME, gripperDeg: GRIPPER_OPEN, source: "sim", runId, move: "home" });
    await log(ctx, "executor", "done", result, runId);
  },
});

export type RunId = Id<"runs">;
