import { useState } from "react";
import { useQuery } from "convex/react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ReferenceLine, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { api } from "../../convex/_generated/api";
import type { Id } from "../../convex/_generated/dataModel";

export default function History() {
  const pours = useQuery(api.pours.recent, { limit: 50 });
  const [expand, setExpand] = useState<Id<"pours"> | null>(null);

  return (
    <section className="surface">
      <div className="section-title">pours</div>
      {!pours ? <div className="small dim">loading…</div>
        : pours.length === 0 ? <div className="small dim">pours appear here after adaptive_pour finishes</div>
          : (
            <table className="table">
              <thead>
                <tr>
                  <th>when</th><th>target</th><th>poured</th><th>error</th>
                  <th>onset / pour °</th><th>mL/s</th>
                </tr>
              </thead>
              <tbody>
                {pours.map((p) => (
                  <>
                    <tr key={p._id}
                        style={{ cursor: "pointer", background: expand === p._id ? "rgba(125,211,252,0.06)" : undefined }}
                        onClick={() => setExpand(expand === p._id ? null : p._id)}>
                      <td className="mono">{new Date(p.createdAt).toLocaleString()}</td>
                      <td className="mono">{p.targetMl.toFixed(2)} mL</td>
                      <td className="mono">{p.pouredMl.toFixed(2)} mL</td>
                      <td className="mono" style={{
                        color: Math.abs(p.errorMl) < 0.3 ? "var(--ok)"
                          : Math.abs(p.errorMl) < 1.0 ? "var(--warn)" : "var(--bad)",
                      }}>
                        {p.errorMl >= 0 ? "+" : ""}{p.errorMl.toFixed(2)}
                      </td>
                      <td className="mono">{p.onsetDeg.toFixed(0)} / {p.pourDeg.toFixed(0)}</td>
                      <td className="mono">{p.measuredMlPerS.toFixed(2)}</td>
                    </tr>
                    {expand === p._id && (
                      <tr key={p._id + "-expand"}>
                        <td colSpan={6} style={{ padding: 0 }}>
                          <PourDetail jobId={p.jobId} target={p.targetMl}
                                      beforeId={p.snapshotBefore} afterId={p.snapshotAfter} />
                        </td>
                      </tr>
                    )}
                  </>
                ))}
              </tbody>
            </table>
          )}
    </section>
  );
}

function PourDetail({ jobId, target, beforeId, afterId }: {
  jobId: Id<"jobs">;
  target: number;
  beforeId?: Id<"_storage">;
  afterId?: Id<"_storage">;
}) {
  const levels = useQuery(api.robot.levelsForJob, { jobId, limit: 400 });
  const t0 = levels?.[0]?.t ?? 0;
  const chartData = (levels ?? []).map((l) => ({
    t: Math.round((l.t - t0) / 100) / 10,
    ml: l.ml,
  }));
  return (
    <div style={{ padding: 12, display: "grid", gap: 12, gridTemplateColumns: "1fr 1fr" }}>
      <div>
        <div className="section-title">level trace</div>
        {chartData.length > 1 ? (
          <ResponsiveContainer width="100%" height={180}>
            <LineChart data={chartData}>
              <CartesianGrid stroke="#1f2531" vertical={false} />
              <XAxis dataKey="t" stroke="#7d869c" tick={{ fontSize: 10 }} />
              <YAxis stroke="#7d869c" tick={{ fontSize: 10 }} width={28} />
              <Tooltip contentStyle={{ background: "#141821", border: "1px solid #262c3b", fontSize: 11 }} />
              <ReferenceLine y={target} stroke="#7dd3fc" strokeDasharray="4 4"
                             label={{ value: "target", fill: "#7dd3fc", fontSize: 10, position: "right" }} />
              <Line type="monotone" dataKey="ml" stroke="#7dd3fc" dot={false} strokeWidth={2} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="small dim">no level samples for this pour</div>
        )}
      </div>
      <div>
        <div className="section-title">before / after</div>
        <div style={{ display: "flex", gap: 8 }}>
          <Snap id={beforeId} label="before" />
          <Snap id={afterId} label="after" />
        </div>
      </div>
    </div>
  );
}

function Snap({ id, label }: { id?: Id<"_storage">; label: string }) {
  const url = useQuery(api.storage.getUrl, id ? { storageId: id } : "skip");
  if (!id) return <div className="small dim" style={{ flex: 1 }}>no {label} snapshot</div>;
  if (!url) return <div className="small dim" style={{ flex: 1 }}>{label}…</div>;
  return (
    <a href={url} target="_blank" rel="noreferrer" style={{ flex: 1 }}>
      <img src={url} alt={label} style={{ width: "100%", borderRadius: 6 }} />
      <div className="small dim" style={{ textAlign: "center", marginTop: 4 }}>{label}</div>
    </a>
  );
}
