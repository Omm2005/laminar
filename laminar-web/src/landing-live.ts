/**
 * Landing page live data: Convex live queries over the Convex browser client (a websocket).
 * Convex pushes a new result whenever telemetry, runs, pours or camera frames change; nothing here polls.
 * The landing page's own script (index.html) defines window.laminarLive, which takes these updates.
 */
import { ConvexClient } from "convex/browser";
import { api } from "../convex/_generated/api";

type Arm = typeof api.site.liveArm._returnType;
type Stats = typeof api.site.liveStats._returnType;
type Frame = typeof api.robot.liveFrame._returnType;

declare global {
  interface Window {
    laminarLive?: {
      arm: (d: Arm, clockOffsetMs: number) => void;
      stats: (s: Stats) => void;
      offline: () => void;
      frame: (cam: "side" | "wrist", f: Frame) => void;
    };
  }
}

const url = import.meta.env.VITE_CONVEX_URL as string;
const site = import.meta.env.VITE_CONVEX_SITE_URL as string;
const client = new ConvexClient(url);

// Telemetry timestamps are server time. One cheap request a minute measures how far this browser's
// clock is from the server's, so the arm interpolates on the right timeline.
let offset = 0;
async function syncClock() {
  try {
    const t0 = Date.now();
    const d = await (await fetch(`${site}/site/arm`, { cache: "no-store" })).json();
    offset = d.now - (t0 + Date.now()) / 2;
  } catch {
    /* keep the last offset */
  }
}
void syncClock();
setInterval(syncClock, 60_000);

client.onUpdate(api.site.liveArm, {}, (d) => window.laminarLive?.arm(d, offset), () => window.laminarLive?.offline());
client.onUpdate(api.site.liveStats, {}, (s) => window.laminarLive?.stats(s), () => window.laminarLive?.offline());
// each camera's newest JPEG bytes, pushed on every frame
for (const cam of ["side", "wrist"] as const) client.onUpdate(api.robot.liveFrame, { cam }, (f) => window.laminarLive?.frame(cam, f));
