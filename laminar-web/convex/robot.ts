import { v } from "convex/values";
import { internalMutation, internalQuery, query } from "./_generated/server";

const CAMS = ["side", "wrist"] as const;

/** Last ~20 joint samples for the 3D arm (it renders slightly behind the newest and interpolates). */
export const recent = query({
  args: {},
  handler: async (ctx) => {
    const rows = await ctx.db.query("telemetry").withIndex("by_t").order("desc").take(20);
    return rows.reverse().map((r) => ({ t: r.t, joints: r.joints, gripperDeg: r.gripperDeg ?? null, source: r.source, move: r.move ?? null }));
  },
});

/** Bench bridge pushes measured joints (see http.ts /bench/telemetry). */
export const push = internalMutation({
  args: { samples: v.array(v.object({ t: v.number(), joints: v.array(v.number()), gripperDeg: v.optional(v.number()), move: v.optional(v.string()) })) },
  handler: async (ctx, { samples }) => {
    for (const s of samples.slice(0, 200)) {
      if (s.joints.length !== 6 || s.joints.some((j) => !Number.isFinite(j))) throw new Error("joints must be 6 finite numbers (degrees)");
      // never in the future: a wrong laptop clock must not hold the 3D arm on a stale pose
      await ctx.db.insert("telemetry", { ...s, t: Math.min(s.t, Date.now()), source: "bench" });
    }
  },
});

/** Keep one hour of joint samples; Tiger Data holds the long history. */
export const prune = internalMutation({
  args: {},
  handler: async (ctx) => {
    // Always keep the newest samples, so the 3D arm still shows its last pose after a quiet hour.
    const keep = new Set((await ctx.db.query("telemetry").withIndex("by_t").order("desc").take(50)).map((r) => r._id));
    const old = await ctx.db.query("telemetry").withIndex("by_t", (q) => q.lt("t", Date.now() - 3600_000)).take(500);
    for (const r of old) if (!keep.has(r._id)) await ctx.db.delete(r._id);
  },
});

/** Remove everything the bench pushed: joint samples, camera frames and the video address (e.g. after a rehearsal). */
export const clearBench = internalMutation({
  args: {},
  handler: async (ctx) => {
    let samples = 0, frames = 0;
    // only bench rows (indexed), so a simulated run writing telemetry at the same time doesn't conflict
    for (const r of await ctx.db.query("telemetry").withIndex("by_source_t", (q) => q.eq("source", "bench")).take(4000)) { await ctx.db.delete(r._id); samples++; }
    for (const f of await ctx.db.query("cameraFrames").collect()) { await ctx.storage.delete(f.storageId); await ctx.db.delete(f._id); frames++; }
    const streams = (await ctx.db.query("streams").collect()).length;
    for (const r of await ctx.db.query("streams").collect()) await ctx.db.delete(r._id);
    return { samples, frames, streams };
  },
});

/** A new frame from a bench camera: keep it and drop all but the two newest for that camera. */
export const saveFrame = internalMutation({
  args: { cam: v.union(v.literal("side"), v.literal("wrist")), storageId: v.id("_storage"), bytes: v.number() },
  handler: async (ctx, { cam, storageId, bytes }) => {
    await ctx.db.insert("cameraFrames", { cam, storageId, bytes, t: Date.now() });
    const old = await ctx.db.query("cameraFrames").withIndex("by_cam_t", (q) => q.eq("cam", cam)).order("desc").collect();
    for (const f of old.slice(2)) { await ctx.storage.delete(f.storageId); await ctx.db.delete(f._id); }
  },
});

/** The bench announces where its live video is. Written at most every 8 s per URL. */
export const setStream = internalMutation({
  args: { url: v.string() },
  handler: async (ctx, { url }) => {
    const row = await ctx.db.query("streams").withIndex("by_name", (q) => q.eq("name", "bench")).first();
    const now = Date.now();
    if (!row) await ctx.db.insert("streams", { name: "bench", url, t: now });
    else if (row.url !== url || now - row.t > 8000) await ctx.db.patch(row._id, { url, t: now });
  },
});

/** The bench's live video address and when it was last confirmed. The page decides if it is fresh. */
export const stream = query({
  args: {},
  handler: async (ctx) => {
    const row = await ctx.db.query("streams").withIndex("by_name", (q) => q.eq("name", "bench")).first();
    return row ? { url: row.url, t: row.t } : null;
  },
});

/** Live query: the newest frame per camera, with a URL the page can show. Updates on every new frame. */
export const cameras = query({
  args: {},
  handler: async (ctx) => {
    const out = [];
    for (const cam of CAMS) {
      const f = await ctx.db.query("cameraFrames").withIndex("by_cam_t", (q) => q.eq("cam", cam)).order("desc").first();
      out.push({ cam, t: f?.t ?? null, url: f ? await ctx.storage.getUrl(f.storageId) : null });
    }
    return out;
  },
});

/** Forget the bench's video address (e.g. a localhost address left by a local test). Frames and joints stay. */
export const clearStream = internalMutation({
  args: { onlyLocal: v.optional(v.boolean()) },
  handler: async (ctx, { onlyLocal }) => {
    let n = 0;
    for (const r of await ctx.db.query("streams").collect()) {
      if (onlyLocal && !/^http:\/\/(localhost|127\.0\.0\.1)/.test(r.url)) continue;
      await ctx.db.delete(r._id); n++;
    }
    return n;
  },
});

const LIVE_CAM = v.union(v.literal("side"), v.literal("wrist"), v.literal("test"));

/** Newest frame for one camera: overwrite its row in place (one write per frame, nothing to clean up). */
export const putFrame = internalMutation({
  args: { cam: LIVE_CAM, jpeg: v.bytes(), captured: v.optional(v.number()) },
  handler: async (ctx, { cam, jpeg, captured }) => {
    const row = await ctx.db.query("liveFrames").withIndex("by_cam", (q) => q.eq("cam", cam)).unique();
    // uploads overlap, so a frame can arrive after a newer one: never step the picture backwards
    if (row && captured !== undefined && row.captured !== undefined && captured <= row.captured) return false;
    const t = Date.now();
    if (row) await ctx.db.patch(row._id, { jpeg, t, captured, seq: row.seq + 1 });
    else await ctx.db.insert("liveFrames", { cam, jpeg, t, captured, seq: 1 });
    return true;
  },
});

/** Live query: one camera's newest JPEG bytes. Each new frame re-runs it and Convex pushes the bytes to the page. */
export const liveFrame = query({
  args: { cam: LIVE_CAM },
  handler: async (ctx, { cam }) => {
    const row = await ctx.db.query("liveFrames").withIndex("by_cam", (q) => q.eq("cam", cam)).unique();
    return row ? { jpeg: row.jpeg, t: row.t, seq: row.seq } : null;
  },
});

/** When the last stored snapshot for a camera was taken (file-storage history, one every few seconds). */
export const lastSnapshot = internalQuery({
  args: { cam: v.union(v.literal("side"), v.literal("wrist")) },
  handler: async (ctx, { cam }) => (await ctx.db.query("cameraFrames").withIndex("by_cam_t", (q) => q.eq("cam", cam)).order("desc").first())?.t ?? 0,
});
