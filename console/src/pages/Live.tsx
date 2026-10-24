import { useMutation, useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ReferenceLine, ResponsiveContainer, CartesianGrid,
} from "recharts";

const JOINT_LIMITS: [number, number][] = [
  [-145, 145], [0, 170], [0, 180], [-80, 90], [-90, 90], [-90, 90],
];
const JOINT_NAMES = ["pan", "lift", "elbow", "wflex", "wyaw", "wroll"];

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
  const latestScale = useQuery(api.scale.latest, {});
  const stopState = useQuery(api.control.stopRequested, {});
  const nodes = useQuery(api.bench.nodes, {});
  const estop = useMutation(api.control.estop);
  const clearStop = useMutation(api.control.clearStop);

  const joints = telem?.joints ?? Array(6).fill(0);
  // merge level + scale samples by their timestamp so both lines share one X axis
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

  return (
    <>
      <div className="card">
        <h2>Current</h2>
        {activeJob ? (
          <p className="mono">
            {activeJob.requestText}
            {" · "}<span className={`pill ${activeJob.status}`}>{activeJob.status}</span>
            {" · step: "}<code>{activeJob.currentStep ?? "—"}</code>
            {" · target "}<code>{activeJob.targetMl.toFixed(2)} mL</code>
          </p>
        ) : <p className="mono">no job</p>}
      </div>

      <div className="row">
        <div className="col" style={{ flex: "2 1 480px" }}>
          <div className="card">
            <h3>Joints (°)</h3>
            <div className="gauges">
              {joints.slice(0, 6).map((v: number, i: number) => {
                const [lo, hi] = JOINT_LIMITS[i];
                const span = hi - lo;
                const pct = ((v - lo) / span) * 100;
                return (
                  <div className="gauge" key={i}>
                    <div className="label">J{i + 1} {JOINT_NAMES[i]}</div>
                    <div className="value">{v.toFixed(1)}</div>
                    <div className="bar"><div style={{ width: 4, marginLeft: -2, left: `${pct}%` }} /></div>
                  </div>
                );
              })}
            </div>
            <p className="mono" style={{ marginTop: 8 }}>
              {telem ? (
                <>pour_deg: {telem.pourDeg?.toFixed(1) ?? "—"} · gripperTorque: {telem.gripperTorque?.toFixed(2) ?? "—"}</>
              ) : "no telemetry yet"}
            </p>
          </div>

          <div className="card">
            <h3>Pour (mL left · grams right, vs s)</h3>
            {activeJob && chartData.length > 1 ? (
              <ResponsiveContainer width="100%" height={240}>
                <LineChart data={chartData}>
                  <CartesianGrid stroke="#262c3b" vertical={false} />
                  <XAxis dataKey="t" stroke="#7d869c" tick={{ fontSize: 11 }} />
                  <YAxis yAxisId="ml" stroke="#7dd3fc" tick={{ fontSize: 11 }} />
                  <YAxis yAxisId="g" orientation="right" stroke="#e8c257" tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={{ background: "#141821", border: "1px solid #262c3b", fontSize: 12 }}
                    labelStyle={{ color: "#c8cedb" }}
                  />
                  <ReferenceLine yAxisId="ml" y={activeJob.targetMl} stroke="#7dd3fc"
                    strokeDasharray="4 4"
                    label={{ value: "target mL", fill: "#7dd3fc", fontSize: 11, position: "right" }} />
                  <Line yAxisId="ml" type="monotone" dataKey="ml" stroke="#7dd3fc" dot={false}
                    strokeWidth={2} connectNulls name="mL" />
                  <Line yAxisId="g" type="monotone" dataKey="grams" stroke="#e8c257" dot={false}
                    strokeWidth={2} connectNulls name="grams" />
                </LineChart>
              </ResponsiveContainer>
            ) : <p className="mono">no samples yet</p>}
          </div>
        </div>

        <div className="col" style={{ flex: "1 1 280px" }}>
          <div className="card">
            <h3>E-Stop</h3>
            {stopState?.stop ? (
              <button className="estop" onClick={() => clearStop({ source: "web" })}>
                CLEAR STOP
              </button>
            ) : (
              <button className="estop danger" onClick={() => estop({ source: "web", message: "manual stop" })}>
                STOP
              </button>
            )}
            {stopState?.stop && (
              <p className="mono" style={{ marginTop: 6 }}>
                stopped since {new Date(stopState.since!).toLocaleTimeString()}
              </p>
            )}
          </div>

          <div className="card">
            <h3>Bench Node</h3>
            {!nodes ? <p className="mono">loading…</p>
              : nodes.length === 0 ? <p className="mono">none registered</p>
                : (
                  <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                    {nodes.map((n) => {
                      const age = (Date.now() - n.lastHeartbeat) / 1000;
                      return (
                        <li key={n._id} style={{
                          display: "flex", alignItems: "center", gap: 10,
                          padding: "8px 0", borderBottom: "1px solid var(--border)",
                        }}>
                          <span style={{
                            width: 10, height: 10, borderRadius: "50%",
                            background: n.online ? "var(--ok)" : "var(--danger)",
                            boxShadow: n.online ? "0 0 6px var(--ok)" : "none",
                            flexShrink: 0,
                          }} />
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div className="mono" style={{ color: "var(--text)" }}>{n.name}</div>
                            <div className="mono" style={{ fontSize: 11, color: "var(--text-dim)" }}>
                              hb {age < 60 ? `${age.toFixed(0)}s` : `${(age / 60).toFixed(0)}m`} ago
                              {" · btn "}{n.lastButton ?? "—"}
                              {" · "}{latestScale ? `${latestScale.grams.toFixed(1)} g` : "— g"}
                            </div>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                )}
          </div>
        </div>
      </div>
    </>
  );
}
