import { internalQuery, mutation, query } from "./_generated/server";
import type { MutationCtx, QueryCtx } from "./_generated/server";
import { v } from "convex/values";
import { internal } from "./_generated/api";

/** The bench computer (bridge/run_plan.py) <-> the dashboard.
 *  The bench computer reports every second what it sees: arm, side camera, level model, calibration, and the
 *  level the model reads in every calibrated box. No report for STALE_MS = "not connected yet". */

export const STALE_MS = 5000;

const part = v.object({ connected: v.boolean(), detail: v.string() });

async function latest(ctx: QueryCtx) {
  return await ctx.db.query("benchStatus").first();
}

export async function readiness(ctx: QueryCtx): Promise<string | null> {
  const s = await latest(ctx);
  if (!s || Date.now() - s.t > STALE_MS) return "the bench computer is not connected yet (start bridge/run_plan.py)";
  if (!s.arm.connected) return `the robot arm is ${s.arm.detail}`;
  if (!s.camera.connected) return `the side camera is ${s.camera.detail}`;
  if (!s.calibration.done) return "the side camera is not calibrated yet: press Calibrate on the Lab page";
  if (s.busy) return `the bench is busy (${s.busy})`;
  return null;
}

export const status = query({
  args: {},
  handler: async (ctx) => {
    const s = await latest(ctx);
    if (!s) return { connected: false as const };
    const connected = Date.now() - s.t <= STALE_MS;
    const frameUrl = connected && s.frameId ? await ctx.storage.getUrl(s.frameId) : null;
    const rawFrameUrl = connected && s.rawFrameId ? await ctx.storage.getUrl(s.rawFrameId) : null;
    const cmd = await ctx.db.query("benchCommands").order("desc").first();
    return { connected, ...s, frameUrl, rawFrameUrl, lastCommand: cmd ?? null };
  },
});

export const readyInternal = internalQuery({ args: {}, handler: async (ctx) => await readiness(ctx) });

/** Bench computer: its report (also writes the camera's container levels into the channels table). */
export const report = mutation({
  args: {
    simulated: v.boolean(), arm: part,
    camera: part,
    model: v.object({ loaded: v.boolean(), detail: v.string() }),
    calibration: v.object({ done: v.boolean(), detail: v.string() }),
    readHz: v.optional(v.number()),
    containers: v.array(v.object({ number: v.number(), box: v.string(), ml: v.number(), conf: v.number(),
                                   capacityMl: v.number(), reachable: v.boolean() })),
    notReady: v.array(v.string()),
    grabs: v.optional(v.array(v.string())),
    busy: v.optional(v.string()),
    frameW: v.optional(v.number()),
    frameH: v.optional(v.number()),
    camIndex: v.optional(v.string()),
    streamPort: v.optional(v.number()),
    boxes: v.optional(v.array(v.array(v.number()))),
  },
  handler: async (ctx, args) => {
    const s = await latest(ctx);
    const row = { ...args, t: Date.now() };
    if (s) await ctx.db.patch(s._id, row);
    else await ctx.db.insert("benchStatus", row);
    // the containers ARE the calibrated boxes; their volume is what the camera reads (when it is sure)
    const existing = await ctx.db.query("channels").collect();
    for (const c of args.containers) {
      const ch = existing.find((e) => e.number === c.number);
      if (!ch) {
        await ctx.db.insert("channels", { number: c.number, contents: `Container ${c.number}`, currentMl: c.ml,
                                          targetMl: 0, capacityMl: c.capacityMl, updatedAt: Date.now() });
      } else if (c.conf >= 0.5 && (Math.abs(ch.currentMl - c.ml) >= 0.02 || ch.capacityMl !== c.capacityMl)) {
        await ctx.db.patch(ch._id, { currentMl: c.ml, capacityMl: c.capacityMl, updatedAt: Date.now() });
      }
    }
    if (args.calibration.done) {          // boxes removed by a new calibration: their containers go too
      for (const ch of existing) {
        if (!args.containers.some((c) => c.number === ch.number)) await ctx.db.delete(ch._id);
      }
    }
  },
});

/** Bench computer: the newest camera picture (with the boxes and levels drawn on). */
export const setFrame = mutation({
  args: { storageId: v.id("_storage"), raw: v.optional(v.boolean()), calib: v.optional(v.boolean()) },
  handler: async (ctx, args) => {
    const s = await latest(ctx);
    if (!s) { await ctx.storage.delete(args.storageId); return; }
    const field = args.calib ? "calibFrameId" : args.raw ? "rawFrameId" : "frameId";
    const old = s[field];
    if (old) await ctx.storage.delete(old);
    await ctx.db.patch(s._id, { [field]: args.storageId });
  },
});

export const uploadUrl = mutation({ args: {}, handler: async (ctx) => await ctx.storage.generateUploadUrl() });

async function command(ctx: MutationCtx, kind: string, args: unknown, needCamera: boolean) {
  const s = await latest(ctx);
  if (!s || Date.now() - s.t > STALE_MS) throw new Error("The bench computer is not connected yet.");
  if (needCamera && !s.camera.connected) throw new Error("The side camera is not connected yet.");
  if (s.busy) throw new Error(`The bench is busy (${s.busy}).`);
  return await ctx.db.insert("benchCommands", { kind, args, status: "pending", createdAt: Date.now() });
}

/** Dashboard: the calibration drawn on the camera picture. Boxes [x, y, w, h] in camera px, one per container. */
export const saveCalibration = mutation({
  args: { boxes: v.array(v.array(v.number())) },
  handler: async (ctx, args) => {
    const s = await latest(ctx);
    const W = s?.frameW ?? 0, H = s?.frameH ?? 0;
    const boxes = args.boxes.map((b) => b.map((n) => Math.round(n)));
    if (!boxes.length) throw new Error("Draw at least one box.");
    for (const [x, y, w, h] of boxes) {
      if (w < 6 || h < 12) throw new Error("A box is too small: drag from the container's rim to its floor.");
      if (x < 0 || y < 0 || (W && x + w > W) || (H && y + h > H)) throw new Error("A box goes outside the picture.");
    }
    boxes.sort((a, b) => a[0] - b[0]);
    return await command(ctx, "set_boxes", { boxes }, true);
  },
});

/** Dashboard: switch the side camera (the numbers can change when cameras are plugged in). */
export const setCamera = mutation({
  args: { index: v.string() },
  handler: async (ctx, args) => await command(ctx, "set_camera", { index: args.index }, false),
});

export const nextCommand = query({
  args: {},
  handler: async (ctx) => await ctx.db.query("benchCommands").withIndex("by_status", (q) => q.eq("status", "pending")).first(),
});

export const updateCommand = mutation({
  args: { id: v.id("benchCommands"), status: v.string(), message: v.optional(v.string()) },
  handler: async (ctx, args) => {
    await ctx.db.patch(args.id, { status: args.status, message: args.message });
    const cmd = await ctx.db.get(args.id);
    if (cmd?.kind === "set_boxes" && args.status === "done") {     // a new calibration: Claude sets up the bench
      await ctx.scheduler.runAfter(1500, internal.agent.setupBench, {});
    }
  },
});

/** Dashboard: ask Claude to look at the calibration again and set up the containers. */
export const requestSetup = mutation({
  args: {},
  handler: async (ctx) => {
    const s = await latest(ctx);
    if (!s?.calibFrameId) throw new Error("Calibrate the side camera first.");
    await ctx.scheduler.runAfter(0, internal.agent.setupBench, {});
  },
});

export const calibInternal = internalQuery({
  args: {},
  handler: async (ctx) => {
    const s = await latest(ctx);
    if (!s) return null;
    return { calibFrameId: s.calibFrameId ?? null, boxes: s.boxes ?? [], frameW: s.frameW ?? null,
             frameH: s.frameH ?? null, containers: s.containers };
  },
});

/** For the agent: which vials have a recorded grab, and which containers the arm can carry to. */
export const capabilitiesInternal = internalQuery({
  args: {},
  handler: async (ctx) => {
    const s = await latest(ctx);
    if (!s) return { grabs: [] as string[], reachable: [] as number[] };
    return { grabs: s.grabs ?? [], reachable: s.containers.filter((c) => c.reachable).map((c) => c.number) };
  },
});
