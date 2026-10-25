/**
 * Bench bridge. The laptop next to the arm polls for approved bench runs and reports progress and each
 * measured pour. Every request needs `Authorization: Bearer $BENCH_TOKEN` (set with `npx convex env set`).
 * There is no endpoint that approves a run: approval only happens in the console, by a named person.
 *
 *   GET  /bench/next                          → { id, tasks, moves } or null
 *   POST /bench/update  { id, status?, step?, reason?, pour?: { tube, well, targetMl, pouredMl } }
 */
import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";

const http = httpRouter();

function authorized(req: Request) {
  const token = process.env.BENCH_TOKEN;
  return !!token && req.headers.get("authorization") === `Bearer ${token}`;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

http.route({
  path: "/bench/next",
  method: "GET",
  handler: httpAction(async (ctx, req) => {
    if (!authorized(req)) return json({ error: "unauthorized" }, 401);
    return json(await ctx.runQuery(internal.runs.nextForBench, {}));
  }),
});

http.route({
  path: "/bench/update",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    if (!authorized(req)) return json({ error: "unauthorized" }, 401);
    try {
      await ctx.runMutation(internal.runs.benchUpdate, await req.json());
      return json({ ok: true });
    } catch (e) {
      return json({ error: (e as Error).message }, 400);
    }
  }),
});

/** Measured joints from the bench (bridge/stream_joints.py style): { samples: [{ t, joints[6], gripperDeg? }] } */
http.route({
  path: "/bench/telemetry",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    if (!authorized(req)) return json({ error: "unauthorized" }, 401);
    try {
      await ctx.runMutation(internal.robot.push, await req.json());
      return json({ ok: true });
    } catch (e) {
      return json({ error: (e as Error).message }, 400);
    }
  }),
});

/**
 * One camera frame from the bench: POST /bench/frame?cam=side|wrist, body = the JPEG bytes (≤ 200 kB; ~25 kB at
 * 480 px is ideal). It overwrites that camera's live row, which a live query pushes to every open page. Every
 * 3 s a copy also goes to file storage, as a short history (cameraFrames).
 */
http.route({
  path: "/bench/frame",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    if (!authorized(req)) return json({ error: "unauthorized" }, 401);
    const params = new URL(req.url).searchParams;
    const cam = params.get("cam");
    if (cam !== "side" && cam !== "wrist" && cam !== "test") return json({ error: "cam must be side or wrist" }, 400);
    const captured = Number(params.get("ts")) || undefined;   // bench ms; lets overlapping uploads keep order
    if (!(req.headers.get("content-type") ?? "").startsWith("image/jpeg")) return json({ error: "send Content-Type: image/jpeg" }, 415);
    const jpeg = await req.arrayBuffer();
    if (jpeg.byteLength === 0 || jpeg.byteLength > 200_000) return json({ error: "frame must be 1 B to 200 kB; send about 480 px wide" }, 413);
    await ctx.runMutation(internal.robot.putFrame, { cam, jpeg, captured });
    if (cam !== "test" && Date.now() - (await ctx.runQuery(internal.robot.lastSnapshot, { cam })) > 3000) {
      const storageId = await ctx.storage.store(new Blob([jpeg], { type: "image/jpeg" }));
      await ctx.runMutation(internal.robot.saveFrame, { cam, storageId, bytes: jpeg.byteLength });
    }
    return json({ ok: true });
  }),
});

/** The bench's live video address: POST /bench/stream {"url": "https://<tunnel>"}. https only (or localhost). */
http.route({
  path: "/bench/stream",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    if (!authorized(req)) return json({ error: "unauthorized" }, 401);
    let url: URL;
    try { url = new URL(String(((await req.json()) as { url?: unknown }).url)); } catch { return json({ error: "send {\"url\": \"https://...\"}" }, 400); }
    const local = url.hostname === "localhost" || url.hostname === "127.0.0.1";
    if (url.protocol !== "https:" && !(local && url.protocol === "http:")) return json({ error: "the stream URL must be https" }, 400);
    await ctx.runMutation(internal.robot.setStream, { url: url.origin });
    return json({ ok: true });
  }),
});

// ---------------------------------------------------------------- Mana Laminar Lab Console

/** Read-only snapshot for the Mana console. No secrets, no personal data beyond operator names. */
http.route({
  path: "/mana/summary",
  method: "GET",
  handler: httpAction(async (ctx, req) => {
    if ((req.headers.get("user-agent") ?? "").startsWith("Laminar-Mana-Console")) await ctx.runMutation(internal.mana.seen, {});
    return json(await ctx.runQuery(internal.mana.summary, {}));
  }),
});

/** Creates a run that still needs a named approval in the console. The token proves the request came from Mana. */
http.route({
  path: "/mana/submit",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    const expected = process.env.MANA_SUBMIT_TOKEN;
    let body: { requestText?: unknown; requestedBy?: unknown; token?: unknown };
    try { body = await req.json(); } catch { return json({ error: "Body must be JSON." }, 400); }
    if (!expected || body.token !== expected) return json({ error: "unauthorized" }, 401);
    if (typeof body.requestText !== "string" || typeof body.requestedBy !== "string") return json({ error: "requestText and requestedBy are required." }, 400);
    try {
      return json(await ctx.runMutation(internal.mana.submit, { requestText: body.requestText, requestedBy: body.requestedBy }));
    } catch (e) {
      return json({ error: (e as Error).message }, 400);
    }
  }),
});

// ---------------------------------------------------------------- public website (hosted in Mana)

const CORS = { "access-control-allow-origin": "*", "access-control-allow-methods": "GET, OPTIONS", "cache-control": "no-store" };

http.route({
  path: "/site/state",
  method: "GET",
  handler: httpAction(async (ctx) =>
    new Response(JSON.stringify(await ctx.runQuery(internal.site.state, {})), { headers: { "content-type": "application/json", ...CORS } })),
});

http.route({
  path: "/site/arm",
  method: "GET",
  // `now` is taken here, per request: inside the query it would be frozen at the last data change,
  // because Convex caches query results until the data they read changes.
  handler: httpAction(async (ctx) =>
    new Response(JSON.stringify({ ...(await ctx.runQuery(internal.site.arm, {})), now: Date.now() }), { headers: { "content-type": "application/json", ...CORS } })),
});

http.route({
  path: "/site/state",
  method: "OPTIONS",
  handler: httpAction(async () => new Response(null, { status: 204, headers: CORS })),
});

export default http;
