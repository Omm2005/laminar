import { memo, useEffect, useRef, useState } from "react";
import { useQuery } from "convex/react";
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

function fmtAgo(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms ago`;
  if (ms < 60_000) return `${Math.round(ms / 1000)} s ago`;
  const m = Math.floor(ms / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return s === 0 ? `${m} m ago` : `${m} m ${s} s ago`;
}

export default function Robot() {
  const jobs = useQuery(api.jobs.list, { limit: 1 });
  const activeJob = jobs?.[0] ?? null;
  const telem = useQuery(api.robot.latestTelemetry, {});
  const levels = useQuery(
    api.robot.levelsForJob,
    activeJob ? { jobId: activeJob._id, limit: 400 } : "skip",
  );

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
  const target = activeJob?.targetMl ?? 0;
  const t0 = levels?.[0]?.t ?? 0;
  const chartData = (levels ?? []).map((l) => ({
    t: Math.round((l.t - t0) / 100) / 10,
    ml: l.ml,
  }));

  const [cameraPreset, setCameraPreset] = useState<"front" | "top" | "shoulder">("front");
  const [showJoints, setShowJoints] = useState(false);

  return (
    <>
      <section className="robot-pane">
        <div className={`robot-canvas ${stale ? "robot-stale" : ""}`}>
          <Arm3D
            samples={buf.current.slice()}
            status={status}
            gripperDeg={telem?.gripperDeg ?? 0}
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

      {activeJob && chartData.length > 1 ? (
        <section className="card">
          <div className="card-title">Pour chart</div>
          <ResponsiveContainer width="100%" height={180}>
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
        </section>
      ) : (
        <section className="card">
          <div className="card-title">Pour chart</div>
          <div className="empty-text">The chart appears while a pour is running.</div>
        </section>
      )}

      {showJoints && (
        <JointsDrawer
          joints={telem?.joints ?? Array(6).fill(0)}
          commanded={telem?.commanded ?? null}
          history={buf.current}
        />
      )}
    </>
  );
}

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
