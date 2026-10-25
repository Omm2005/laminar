import { memo, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ReferenceLine, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { api } from "../../convex/_generated/api";
import Arm3D, { type Telem } from "../robot/Arm3D";

const BUFFER_LEN = 20;
const STALE_MS = 2000;
const JOINT_LIMITS: [number, number][] = [
  [-145, 145], [0, 170], [0, 180], [-80, 90], [-90, 90], [-90, 90],
];

/* ----------------------------------------------------------- plain words */

function vialLetter(n: number): string {
  return String.fromCharCode(64 + Math.max(1, Math.min(26, n))); // 1 -> A
}
function jobLine(job?: { targetMl: number; channel: number; vial: number } | null): string {
  if (!job || job.targetMl <= 0) return "No job";
  return `Pour ${job.targetMl.toFixed(1)} mL into channel ${job.channel} from vial ${vialLetter(job.vial)}`;
}
function stepInWords(
  status: string,
  stepName: string | null | undefined,
  job: { channel: number; vial: number; targetMl: number } | null,
  poured: number | null,
): string {
  if (status === "awaiting_approval") return "Waiting for approval";
  if (status === "planning")  return "Planning the pour";
  if (status === "queued")    return "In the queue";
  if (status === "approved")  return "Ready to run";
  if (status === "done")      return "Done";
  if (status === "failed")    return "Failed";
  if (status === "stalled")   return "Stalled";
  if (status === "estop")     return "Stopped";
  if (!job) return "Idle";
  const V = vialLetter(job.vial);
  const C = job.channel;
  switch (stepName) {
    case "grasp":         return `Grasping vial ${V}`;
    case "lift":          return "Lifting";
    case "traverse":      return `Moving over channel ${C}`;
    case "lower":         return `Lowering over channel ${C}`;
    case "adaptive_pour": return poured == null
      ? `Starting the pour into channel ${C}`
      : `Pouring ${poured.toFixed(1)} / ${job.targetMl.toFixed(1)} mL`;
    case "untilt":        return "Returning to upright";
    case "return_vial":   return `Carrying vial ${V} back`;
    case "home":          return "Returning home";
    default:              return status === "running" ? "Running" : "Standing by";
  }
}

function statusLabel(status: string): string {
  if (status === "awaiting_approval") return "Awaiting approval";
  if (status === "idle") return "Idle";
  if (status === "estop") return "Stopped";
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function fmtAgo(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms ago`;
  if (ms < 60_000) return `${Math.round(ms / 1000)} s ago`;
  const m = Math.floor(ms / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return s === 0 ? `${m} m ago` : `${m} m ${s} s ago`;
}

function eventPhrase(e: { kind: string; source: string; message: string }): string {
  // Rewrite the raw message into plain words; fall back to a lightly cleaned version.
  const m = e.message;
  const src = e.source === "web" ? "the console"
            : e.source === "bench" ? "the bench"
            : e.source === "robot" ? "the robot"
            : e.source === "planner" ? "the planner"
            : e.source === "watchdog" ? "the watchdog"
            : e.source;

  if (m.startsWith("status -> ")) {
    const rest = m.slice("status -> ".length).replace(/_/g, " ");
    return `${capitalize(rest)}`;
  }
  if (m.startsWith("job submitted: ")) {
    return `Job submitted: ${m.slice("job submitted: ".length)}`;
  }
  if (m.startsWith("plan ready: ")) {
    return `Plan ready — ${m.slice("plan ready: ".length)}`;
  }
  if (m.startsWith("job approved")) {
    return m.replace(/^job approved/, "Approved");
  }
  if (m.startsWith("job rejected")) {
    return m.replace(/^job rejected/, "Rejected");
  }
  if (m.includes("stop cleared")) {
    return `Stop cleared by ${src}`;
  }
  if (e.kind === "estop") {
    return `Stop requested by ${src}`;
  }
  if (m.startsWith("robot client picked up job")) {
    return "The robot picked up the job";
  }
  return capitalize(m.replace(/_/g, " "));
}
function capitalize(s: string): string {
  return s.length ? s[0].toUpperCase() + s.slice(1) : s;
}

/* ----------------------------------------------------------- page */

export default function Live() {
  const jobs = useQuery(api.jobs.list, { limit: 1 });
  const activeJob = jobs?.[0] ?? null;
  const telem = useQuery(api.robot.latestTelemetry, {});
  const levels = useQuery(
    api.robot.levelsForJob,
    activeJob ? { jobId: activeJob._id, limit: 400 } : "skip",
  );
  const scale = useQuery(
    api.scale.forJob,
    activeJob ? { jobId: activeJob._id, limit: 400 } : "skip",
  );
  const stopState = useQuery(api.control.stopRequested, {});
  const events = useQuery(api.events.recent, { limit: 20 });
  const estop = useMutation(api.control.estop);
  const clearStop = useMutation(api.control.clearStop);
  const approve = useMutation(api.jobs.approve);

  // telemetry ring buffer for the 3D arm
  const buf = useRef<Telem[]>([]);
  const [, setBufTick] = useState(0);
  useEffect(() => {
    if (!telem?.joints) return;
    const b = buf.current;
    if (b.length === 0 || b[b.length - 1].t !== telem.t) {
      b.push({ t: telem.t, joints: telem.joints, commanded: telem.commanded });
      if (b.length > BUFFER_LEN) b.shift();
      setBufTick((n) => n + 1);
    }
  }, [telem?.t]);

  // staleness tick (every 500 ms is enough)
  const [, setNowTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setNowTick((n) => n + 1), 500);
    return () => clearInterval(id);
  }, []);
  const now = Date.now();
  const lastT = telem?.t ?? 0;
  const sinceMs = lastT ? now - lastT : Infinity;
  const stale = sinceMs > STALE_MS;

  const status = activeJob?.status ?? "idle";
  const stopped = !!stopState?.stop;

  // poured so far during the active job (latest level - first level sample)
  const poured = levels && levels.length >= 1
    ? Math.max(0, (levels[levels.length - 1].ml) - (levels[0].ml))
    : null;

  // chart data: merge level + scale by time
  const t0 = levels?.[0]?.t ?? scale?.[0]?.t ?? 0;
  type Point = { t: number; ml?: number; grams?: number };
  const byT = new Map<number, Point>();
  for (const l of levels ?? []) {
    const t = Math.round((l.t - t0) / 100) / 10;
    const p = byT.get(t) ?? { t };
    p.ml = l.ml;
    byT.set(t, p);
  }
  for (const s of scale ?? []) {
    const t = Math.round((s.t - t0) / 100) / 10;
    const p = byT.get(t) ?? { t };
    p.grams = s.grams;
    byT.set(t, p);
  }
  const chartData = Array.from(byT.values()).sort((a, b) => a.t - b.t);

  const [cameraPreset, setCameraPreset] = useState<"front" | "top" | "shoulder">("front");
  const [showJoints, setShowJoints] = useState(false);

  const target = activeJob?.targetMl ?? 0;
  const progress = target > 0 && poured != null ? Math.min(1, poured / target) : 0;

  return (
    <>
      {/* STATUS BAR */}
      <section className="status-bar">
        <div className="sb-left">
          <div className="sb-pill" data-status={status}>{statusLabel(status)}</div>
          <div className="sb-job">{jobLine(activeJob)}</div>
        </div>
        <div className="sb-center">{stepInWords(status, activeJob?.currentStep, activeJob, poured)}</div>
        <div className="sb-right">
          {activeJob?.status === "awaiting_approval" && (
            <button className="btn-primary"
                    onClick={() => approve({ jobId: activeJob._id, approvedBy: "hema" })}>
              Approve
            </button>
          )}
          {stopped ? (
            <button className="btn-stop-mini" onClick={() => clearStop({ source: "web" })}>Clear stop</button>
          ) : (
            <button className="btn-stop-mini" onClick={() => estop({ source: "web", message: "manual" })}>Stop</button>
          )}
          <LiveBadge stale={stale} sinceMs={sinceMs} />
        </div>
      </section>

      {/* MAIN GRID */}
      <div className="live-grid">
        {/* ROBOT (hero, 60%) */}
        <section className="robot-pane">
          <div className={`robot-canvas ${stale ? "robot-stale" : ""}`}>
            <Arm3D
              samples={buf.current.slice()}
              status={status}
              gripperDeg={0}
              pourChannel={activeJob?.channel ?? null}
              cameraPreset={cameraPreset}
            />
          </div>
          {stale && (
            <div className="stale-overlay">
              <div className="stale-head">No live data</div>
              <div className="stale-sub">
                last seen {lastT ? fmtAgo(sinceMs) : "never"} · start the bench
              </div>
              <div className="stale-hint mono">
                powershell -ExecutionPolicy Bypass -File console\bridge\start_bench.ps1
              </div>
            </div>
          )}
          <div className="cam-chips">
            {(["front", "top", "shoulder"] as const).map((k) => (
              <button key={k} className={cameraPreset === k ? "chip chip-on" : "chip"}
                      onClick={() => setCameraPreset(k)}>{k}</button>
            ))}
          </div>
          <button className="details-toggle" onClick={() => setShowJoints((v) => !v)}>
            {showJoints ? "Hide details" : "Details"}
          </button>
        </section>

        {/* RIGHT COLUMN: Pour · Bench · Activity */}
        <aside className="right-col">
          <PourBlock target={target} poured={poured} progress={progress}
                     chartData={chartData} active={!!activeJob} />
          <BenchBlock activeChannel={activeJob?.channel ?? null}
                      poured={poured} target={target} activeVial={activeJob?.vial ?? null} />
          <ActivityBlock events={events ?? []} />
        </aside>
      </div>

      {/* joints hidden behind Details drawer */}
      {showJoints && (
        <JointsDrawer joints={telem?.joints ?? Array(6).fill(0)}
                      commanded={telem?.commanded ?? null}
                      history={buf.current} />
      )}
    </>
  );
}

/* ----------------------------------------------------------- top bar */

function LiveBadge({ stale, sinceMs }: { stale: boolean; sinceMs: number }) {
  if (stale) return null;
  return <span className="live-pill">LIVE · {Math.round(sinceMs)} ms</span>;
}

/* ----------------------------------------------------------- Pour block */

function PourBlock({
  target, poured, progress, chartData, active,
}: {
  target: number; poured: number | null; progress: number;
  chartData: { t: number; ml?: number; grams?: number }[];
  active: boolean;
}) {
  return (
    <section className="card">
      <div className="card-title">Pour</div>
      <div className="pour-head">
        <ProgressRing progress={progress} />
        <div className="pour-num">
          <div className="n">{(poured ?? 0).toFixed(1)}</div>
          <div className="d">of {target.toFixed(1)} mL</div>
        </div>
      </div>
      {active && chartData.length > 1 ? (
        <ResponsiveContainer width="100%" height={160}>
          <LineChart data={chartData} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
            <CartesianGrid stroke="#1f2531" vertical={false} />
            <XAxis dataKey="t" stroke="#7d869c" tick={{ fontSize: 10 }} />
            <YAxis stroke="#7d869c" tick={{ fontSize: 10 }} width={28} />
            <Tooltip contentStyle={{ background: "#141821", border: "1px solid #262c3b", fontSize: 11 }} />
            <ReferenceLine y={target} stroke="#7dd3fc" strokeDasharray="4 4" />
            <Line type="monotone" dataKey="ml" stroke="#7dd3fc" dot={false} strokeWidth={2}
                  connectNulls isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      ) : (
        <div className="empty-text">Chart starts when the pour starts.</div>
      )}
    </section>
  );
}

function ProgressRing({ progress }: { progress: number }) {
  const r = 32;
  const circ = 2 * Math.PI * r;
  const off = circ * (1 - Math.max(0, Math.min(1, progress)));
  return (
    <svg width={80} height={80} viewBox="0 0 80 80" className="ring">
      <circle cx={40} cy={40} r={r} fill="none" stroke="#1f2531" strokeWidth={6} />
      <circle cx={40} cy={40} r={r} fill="none" stroke="var(--accent)" strokeWidth={6}
              strokeDasharray={circ} strokeDashoffset={off}
              transform="rotate(-90 40 40)" strokeLinecap="round" />
    </svg>
  );
}

/* ----------------------------------------------------------- Bench block */

function BenchBlock({ activeChannel, poured, target, activeVial }: {
  activeChannel: number | null; poured: number | null; target: number; activeVial: number | null;
}) {
  // placeholder bench state until the BENCH schema lands: 4 vials, 12 channels at 0 mL.
  const vials = [1, 2, 3, 4];
  const channels = Array.from({ length: 12 }, (_, i) => i + 1);
  const CAPACITY = 10;
  return (
    <section className="card">
      <div className="card-title">Bench</div>
      <div className="bench-vials">
        {vials.map((v) => (
          <div key={v} className={`vial ${activeVial === v ? "vial-active" : ""}`}>
            <div className="vial-id">{vialLetter(v)}</div>
            <div className="vial-sub">—</div>
          </div>
        ))}
      </div>
      <svg viewBox="0 0 480 100" className="reservoir" preserveAspectRatio="xMidYMid meet">
        {channels.map((c, i) => {
          const x = 20 + i * 36;
          const isActive = activeChannel === c;
          const fillMl = isActive && poured != null ? Math.min(CAPACITY, poured) : 0;
          const targetMl = isActive ? target : 0;
          const h = 70;
          const fillH = (fillMl / CAPACITY) * h;
          const targetY = h - (targetMl / CAPACITY) * h;
          return (
            <g key={c}>
              <rect x={x} y={20} width={28} height={h} rx={3}
                    fill="#1b2130" stroke={isActive ? "var(--accent)" : "#262c3b"} />
              {fillH > 0 && (
                <rect x={x} y={20 + h - fillH} width={28} height={fillH} rx={3}
                      fill="var(--accent)" opacity={0.7} />
              )}
              {isActive && target > 0 && (
                <line x1={x - 2} x2={x + 30} y1={20 + targetY} y2={20 + targetY}
                      stroke="#e8c257" strokeWidth={1.5} strokeDasharray="3 2" />
              )}
              <text x={x + 14} y={14} textAnchor="middle" fontSize="10" fill="#7d869c">{c}</text>
            </g>
          );
        })}
      </svg>
      <div className="bench-hint small dim">
        {activeChannel
          ? `Channel ${activeChannel} is active · target ${target.toFixed(1)} mL`
          : "Awaiting a job"}
      </div>
    </section>
  );
}

/* ----------------------------------------------------------- Activity block */

function ActivityBlock({ events }: { events: { _id: string; t: number; kind: string; source: string; message: string }[] }) {
  const listRef = useRef<HTMLUListElement | null>(null);
  const atTop = useRef(true);
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    if (atTop.current) el.scrollTop = 0;
  }, [events]);
  function onScroll() {
    const el = listRef.current;
    if (!el) return;
    atTop.current = el.scrollTop < 10;
  }
  const now = Date.now();
  return (
    <section className="card">
      <div className="card-title">Activity</div>
      {events.length === 0 ? (
        <div className="empty-text">
          Events will show up here as the planner, the robot and the bench do things.
        </div>
      ) : (
        <ul className="activity" ref={listRef} onScroll={onScroll}>
          {events.map((e) => (
            <li key={e._id}>
              <span className={`dot ${e.kind === "error" ? "bad" : e.kind === "warn" ? "warn" : "ok"}`} />
              <span>{eventPhrase(e)}</span>
              <span className="dim small ml-auto num">{fmtAgo(now - e.t)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/* ----------------------------------------------------------- Details drawer */

const JointsDrawer = memo(function JointsDrawer({
  joints, commanded, history,
}: { joints: number[]; commanded: number[] | null; history: Telem[] }) {
  return (
    <section className="card">
      <div className="card-title">Joint details</div>
      <div className="joints-grid">
        {joints.slice(0, 6).map((v, i) => {
          const [lo, hi] = JOINT_LIMITS[i];
          const span = hi - lo || 1;
          const pct = ((v - lo) / span) * 100;
          const cmd = commanded?.[i];
          return (
            <div className="joint" key={i}>
              <div className="j-head">
                <span className="dim small">Joint {i + 1}</span>
                <span className="j-val num">{v.toFixed(1)}°</span>
              </div>
              {cmd != null && (
                <div className="j-sub small dim num">target {cmd.toFixed(1)}°</div>
              )}
              <Spark values={history.map((s) => s.joints[i] ?? 0)} min={lo} max={hi} />
              <div className="j-bar"><div style={{ left: `${pct}%` }} /></div>
            </div>
          );
        })}
      </div>
    </section>
  );
});

function Spark({ values, min, max }: { values: number[]; min: number; max: number }) {
  const w = 80, h = 18;
  if (values.length < 2) return <svg width={w} height={h} />;
  const span = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = h - ((v - min) / span) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return (
    <svg width={w} height={h} className="spark">
      <polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
    </svg>
  );
}
