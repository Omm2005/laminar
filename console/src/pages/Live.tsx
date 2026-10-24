import { memo, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ReferenceLine, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { api } from "../../convex/_generated/api";
import Arm3D, { type Telem } from "../robot/Arm3D";

const JOINT_LIMITS: [number, number][] = [
  [-145, 145], [0, 170], [0, 180], [-80, 90], [-90, 90], [-90, 90],
];
const BUFFER_LEN = 20; // 20 samples at 10 Hz = 2 s

export default function Live() {
  const jobs = useQuery(api.jobs.list, { limit: 1 });
  const activeJob = jobs?.[0];
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
  const nodes = useQuery(api.bench.nodes, {});
  const events = useQuery(api.events.recent, { limit: 20 });
  const estop = useMutation(api.control.estop);
  const clearStop = useMutation(api.control.clearStop);
  const approve = useMutation(api.jobs.approve);
  const reject = useMutation(api.jobs.reject);

  // ring buffer of telemetry samples for the 3D view + sparklines
  const buf = useRef<Telem[]>([]);
  const [bufTick, setBufTick] = useState(0);
  useEffect(() => {
    if (!telem?.joints) return;
    const sample: Telem = { t: telem.t, joints: telem.joints, commanded: telem.commanded };
    const b = buf.current;
    // dedupe on timestamp
    if (b.length === 0 || b[b.length - 1].t !== sample.t) {
      b.push(sample);
      if (b.length > BUFFER_LEN) b.shift();
      setBufTick((n) => n + 1);
    }
  }, [telem?.t]);

  // top-bar latency: now - newest telemetry.t in ms
  const [latency, setLatency] = useState<number | null>(null);
  useEffect(() => {
    const id = setInterval(() => {
      if (telem?.t) setLatency(Date.now() - telem.t);
      else setLatency(null);
    }, 150);
    return () => clearInterval(id);
  }, [telem?.t]);

  const status = activeJob?.status ?? "idle";
  const joints = telem?.joints ?? Array(6).fill(0);

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

  return (
    <>
      {/* top bar */}
      <section className="surface topbar">
        <StatusPill status={status} />
        <div className="step">
          <span className="dim">step</span>{" "}
          <span className="mono">{activeJob?.currentStep ?? "—"}</span>
        </div>
        <LatencyBadge ms={latency} />
      </section>

      {/* main: 3D arm full width (desktop keeps 3D on left, column on right) */}
      <div className="live-grid">
        <section className="surface arm-pane">
          <Arm3D
            samples={buf.current.slice()}
            status={status}
            gripperDeg={0}
            pourChannel={activeJob?.channel ?? null}
            cameraPreset={cameraPreset}
          />
          <div className="arm-controls">
            {(["front", "top", "shoulder"] as const).map((k) => (
              <button key={k}
                className={cameraPreset === k ? "chip chip-on" : "chip"}
                onClick={() => setCameraPreset(k)}>
                {k}
              </button>
            ))}
          </div>
        </section>

        <aside className="live-side">
          <StopPanel stopped={!!stopState?.stop}
                     onStop={() => estop({ source: "web", message: "manual" })}
                     onClear={() => clearStop({ source: "web" })} />

          <BigPoured poured={0} target={activeJob?.targetMl ?? 0} />

          <section className="surface">
            <div className="section-title">pour (mL · g vs s)</div>
            {activeJob && chartData.length > 1 ? (
              <ResponsiveContainer width="100%" height={180}>
                <LineChart data={chartData}>
                  <CartesianGrid stroke="#1f2531" vertical={false} />
                  <XAxis dataKey="t" stroke="#7d869c" tick={{ fontSize: 10 }} />
                  <YAxis yAxisId="ml" stroke="#7dd3fc" tick={{ fontSize: 10 }} width={28} />
                  <YAxis yAxisId="g" orientation="right" stroke="#e8c257" tick={{ fontSize: 10 }} width={28} />
                  <Tooltip contentStyle={{ background: "#141821", border: "1px solid #262c3b", fontSize: 11 }} />
                  <ReferenceLine yAxisId="ml" y={activeJob.targetMl} stroke="#7dd3fc"
                    strokeDasharray="4 4" />
                  <Line yAxisId="ml" type="monotone" dataKey="ml" stroke="#7dd3fc" dot={false} strokeWidth={2}
                    connectNulls isAnimationActive={false} />
                  <Line yAxisId="g" type="monotone" dataKey="grams" stroke="#e8c257" dot={false} strokeWidth={2}
                    connectNulls isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <EmptyWhy>will populate when a pour is running (adaptive_pour pushes level samples at 5 Hz)</EmptyWhy>
            )}
          </section>

          <Approve activeJob={activeJob}
                   onApprove={(name) => activeJob && approve({ jobId: activeJob._id, approvedBy: name })}
                   onReject={(name) => activeJob && reject({ jobId: activeJob._id, approvedBy: name })} />

          <section className="surface">
            <div className="section-title">joints (°)</div>
            <Gauges joints={joints} history={buf.current} key={bufTick} />
          </section>

          <BenchNodes nodes={nodes} />
        </aside>
      </div>

      <section className="surface">
        <div className="section-title">events</div>
        <EventFeed events={events ?? []} />
      </section>
    </>
  );
}

/* ------------------------------- top bar ------------------------------- */

function StatusPill({ status }: { status: string }) {
  return <span className={`pill ${status}`}>{status.replace("_", " ")}</span>;
}
function LatencyBadge({ ms }: { ms: number | null }) {
  if (ms == null) return <span className="pill">no telemetry</span>;
  const cls = ms < 300 ? "ok" : ms < 800 ? "warn" : "bad";
  return <span className={`latency ${cls}`}>{Math.round(ms)} ms</span>;
}

/* ------------------------------- side column ------------------------------- */

function StopPanel({ stopped, onStop, onClear }: { stopped: boolean; onStop: () => void; onClear: () => void }) {
  return (
    <section className="surface">
      {stopped ? (
        <button className="btn-stop btn-stop-clear" onClick={onClear}>CLEAR STOP</button>
      ) : (
        <button className="btn-stop" onClick={onStop}>STOP</button>
      )}
    </section>
  );
}

function BigPoured({ poured, target }: { poured: number; target: number }) {
  return (
    <section className="surface big">
      <div className="dim small">POURED</div>
      <div className="big-num">
        {poured.toFixed(2)}
        <span className="dim medium"> / {target.toFixed(2)} mL</span>
      </div>
    </section>
  );
}

function Approve({ activeJob, onApprove, onReject }: {
  activeJob?: { _id: string; status: string; requestText: string };
  onApprove: (name: string) => void;
  onReject: (name: string) => void;
}) {
  const [name, setName] = useState("hema");
  if (!activeJob || activeJob.status !== "awaiting_approval") return null;
  return (
    <section className="surface approve">
      <div className="section-title">awaiting approval</div>
      <div className="mono dim small">{activeJob.requestText}</div>
      <div className="row" style={{ gap: 8, marginTop: 8 }}>
        <input value={name} onChange={(e) => setName(e.target.value)}
               style={{ maxWidth: 120 }} />
        <button className="btn-primary" onClick={() => onApprove(name)}>Approve</button>
        <button className="btn-ghost" onClick={() => onReject(name)}>Reject</button>
      </div>
    </section>
  );
}

const Gauges = memo(function Gauges({ joints, history }: { joints: number[]; history: Telem[] }) {
  return (
    <div className="gauges">
      {joints.slice(0, 6).map((v, i) => {
        const [lo, hi] = JOINT_LIMITS[i];
        const span = hi - lo || 1;
        const pct = ((v - lo) / span) * 100;
        return (
          <div className="gauge" key={i}>
            <div className="g-head">
              <span className="dim small">J{i + 1}</span>
              <span className="g-val">{v.toFixed(1)}</span>
            </div>
            <Spark values={history.map((s) => s.joints[i] ?? 0)} min={lo} max={hi} />
            <div className="g-bar"><div style={{ left: `${pct}%` }} /></div>
          </div>
        );
      })}
    </div>
  );
});

function Spark({ values, min, max }: { values: number[]; min: number; max: number }) {
  const w = 60, h = 18;
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

function BenchNodes({ nodes }: { nodes?: { _id: string; name: string; lastHeartbeat: number; lastButton?: string; online?: boolean }[] }) {
  const latestScale = useQuery(api.scale.latest, {});
  if (!nodes) return null;
  if (nodes.length === 0) return null;
  return (
    <section className="surface">
      <div className="section-title">bench</div>
      <ul className="bench-list">
        {nodes.map((n) => {
          const age = (Date.now() - n.lastHeartbeat) / 1000;
          return (
            <li key={n._id}>
              <span className={`dot ${n.online ? "ok" : "bad"}`} />
              <div className="col-grow">
                <div className="mono">{n.name}</div>
                <div className="small dim mono">
                  hb {age < 60 ? `${age.toFixed(0)}s` : `${(age / 60).toFixed(0)}m`}
                  {" · btn "}{n.lastButton ?? "—"}
                  {" · "}{latestScale ? `${latestScale.grams.toFixed(1)} g` : "— g"}
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function EventFeed({ events }: { events: { _id: string; t: number; kind: string; source: string; message: string }[] }) {
  if (events.length === 0) return <EmptyWhy>planner, approvals, robot steps, and e-stops appear here</EmptyWhy>;
  return (
    <ul className="event-feed">
      {events.map((e) => (
        <li key={e._id}>
          <span className={`dot ${e.kind === "error" ? "bad" : e.kind === "warn" ? "warn" : "ok"}`} />
          <span className="mono small dim" style={{ minWidth: 60 }}>
            {new Date(e.t).toLocaleTimeString()}
          </span>
          <span className="small dim" style={{ minWidth: 80 }}>{e.source}</span>
          <span className="small">{e.message}</span>
        </li>
      ))}
    </ul>
  );
}

function EmptyWhy({ children }: { children: React.ReactNode }) {
  return <div className="empty-why small dim">{children}</div>;
}
