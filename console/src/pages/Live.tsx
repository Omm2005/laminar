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
  const telem = useQuery(api.robot.latestTelemetry, { jobId: activeJob?._id });
  const levels = useQuery(
    api.robot.levelsForJob,
    activeJob ? { jobId: activeJob._id, limit: 400 } : "skip",
  );
  const stopState = useQuery(api.control.stopRequested, {});
  const nodes = useQuery(api.bench.nodes, {});
  const estop = useMutation(api.control.estop);
  const clearStop = useMutation(api.control.clearStop);

  const joints = telem?.joints ?? Array(6).fill(0);
  const levelData = (levels ?? []).map((l) => ({
    t: Math.round((l.t - (levels![0]?.t ?? l.t)) / 100) / 10, // seconds since start, 1dp
    ml: l.ml,
  }));

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
            <h3>Pour level (mL vs s)</h3>
            {activeJob && levelData.length > 1 ? (
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={levelData}>
                  <CartesianGrid stroke="#262c3b" vertical={false} />
                  <XAxis dataKey="t" stroke="#7d869c" tick={{ fontSize: 11 }} />
                  <YAxis stroke="#7d869c" tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={{ background: "#141821", border: "1px solid #262c3b", fontSize: 12 }}
                    labelStyle={{ color: "#c8cedb" }}
                  />
                  <ReferenceLine y={activeJob.targetMl} stroke="#e8c257" strokeDasharray="4 4"
                    label={{ value: "target", fill: "#e8c257", fontSize: 11, position: "right" }} />
                  <Line type="monotone" dataKey="ml" stroke="#7dd3fc" dot={false} strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            ) : <p className="mono">no level samples yet</p>}
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
            <h3>Bench nodes</h3>
            {!nodes ? <p className="mono">loading…</p>
              : nodes.length === 0 ? <p className="mono">none registered</p>
                : (
                  <table className="table">
                    <thead><tr><th>name</th><th>heartbeat</th><th>last btn</th></tr></thead>
                    <tbody>
                      {nodes.map((n) => {
                        const age = (Date.now() - n.lastHeartbeat) / 1000;
                        const stale = age > 5;
                        return (
                          <tr key={n._id}>
                            <td className="mono">{n.name}</td>
                            <td className="mono" style={{ color: stale ? "var(--danger)" : "var(--ok)" }}>
                              {age < 60 ? `${age.toFixed(0)}s ago` : `${(age / 60).toFixed(0)}m ago`}
                            </td>
                            <td className="mono">{n.lastButton ?? "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
          </div>
        </div>
      </div>
    </>
  );
}
