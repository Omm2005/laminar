import { useCallback, useEffect, useState } from "react";
import { useAction } from "convex/react";
import { api } from "../convex/_generated/api";
import { CompareBars, DYE, ago, fmtSigned } from "./ui";
import ArmRuns from "./ArmRuns";

type Res<T> = T | undefined;
const ok = <T extends { configured: boolean }>(r: T | undefined): r is Extract<T, { configured: true }> => !!r && r.configured;

const METRICS: Record<string, string> = { rms_error: "RMS tracking error", max_error: "Max tracking error", peak_acc: "Peak acceleration", rms_jerk: "RMS jerk" };

/** Tiger Data reads, through Convex actions. Refreshes every 10 s while open. */
export default function Data() {
  const overview = useAction(api.tiger.overview);
  const quality = useAction(api.tiger.quality);
  const drift = useAction(api.tiger.drift);
  const arm = useAction(api.tiger.armTuning);
  const [o, setO] = useState<Res<Awaited<ReturnType<typeof overview>>>>();
  const [q, setQ] = useState<Res<Awaited<ReturnType<typeof quality>>>>();
  const [d, setD] = useState<Res<Awaited<ReturnType<typeof drift>>>>();
  const [a, setA] = useState<Res<Awaited<ReturnType<typeof arm>>>>();
  const [metric, setMetric] = useState("rms_error");
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const [o1, q1, d1] = await Promise.all([overview({}), quality({ hours: 48 }), drift({})]);
      setO(o1); setQ(q1); setD(d1); setErr("");
    } catch (e) { setErr((e as Error).message.split("\n")[0]); }
  }, [overview, quality, drift]);
  useEffect(() => {
    void load();
    arm({}).then(setA).catch(() => {});
    const t = setInterval(load, 10_000);
    return () => clearInterval(t);
  }, [load, arm]);

  if (o && !ok(o)) return <p className="notice warn">Tiger Data isn’t connected. Set it with <code>npx convex env set TIGER_DATABASE_URL "postgres://…"</code>; the console keeps working without it.</p>;
  const rows = ok(a) ? a.rows.filter((r) => r.metric === metric) : [];
  const unit = rows[0]?.unit ?? "";

  return (
    <div className="stack">
      <div>
        <p className="label">The real arm, from Tiger Data</p>
        <h3 style={{ fontSize: "1.375rem", marginTop: 6 }}>Every run, every joint, 100 times a second</h3>
      </div>
      <ArmRuns />
      <div style={{ marginTop: 12 }}>
        <p className="label">Pours from this console</p>
      </div>
      {err && <p className="notice bad">Couldn’t reach Tiger: {err}. Pours are still saved in Convex.</p>}
      <div className="tiles">
        <div><span className="label">Pours in Tiger</span><b>{ok(o) ? o.pours : "…"}</b><small>{ok(o) ? `${o.pours1h} in the last hour` : ""}</small></div>
        <div><span className="label">Events in Tiger</span><b>{ok(o) ? o.events : "…"}</b><small>audit mirror</small></div>
        <div><span className="label">Last pour</span><b style={{ fontSize: "1.1rem" }}>{ok(o) && o.lastPour ? ago(o.lastPour) : "—"}</b></div>
        <div><span className="label">Query time</span><b>{ok(o) ? `${o.queryMs} ms` : "…"}</b><small>Convex action → Tiger</small></div>
      </div>

      <div className="grid2">
        <div className="panel">
          <p className="label">Hypertables</p>
          <div className="tbl"><table>
            <thead><tr><th>Table</th><th className="num">Chunks</th><th className="num">Size</th><th>Compression</th></tr></thead>
            <tbody>{ok(o) && o.tables.map((t) => (
              <tr key={t.name}><td><code>{t.name}</code></td><td className="num">{t.chunks}</td><td className="num">{(t.bytes / 1024).toFixed(0)} KB</td><td>{t.compression ? "after 7 days" : "off"}</td></tr>
            ))}</tbody>
          </table></div>
          <p className="label" style={{ marginTop: 14 }}>Background jobs</p>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18, fontSize: "0.875rem" }}>{ok(o) && o.policies.map((p) => <li key={p.name}>{p.name} · every {p.every}</li>)}</ul>
        </div>
        <div className="panel">
          <p className="label">Drift check · last 10 pours vs all earlier, per tube</p>
          <div className="tbl"><table>
            <thead><tr><th>Tube</th><th className="num">Recent mean</th><th className="num">Earlier mean</th><th>Verdict</th></tr></thead>
            <tbody>{ok(d) && d.rows.map((r) => {
              const shift = r.recent !== null && r.before !== null ? r.recent - r.before : null;
              const drifting = shift !== null && Math.abs(shift) > 0.03 && r.beforeN >= 5;
              return (
                <tr key={r.tube}><td><span className="dot" style={{ background: DYE[r.tube] }} /> {r.tube}</td>
                  <td className="num">{fmtSigned(r.recent)} <span className="muted">n={r.recentN}</span></td>
                  <td className="num">{fmtSigned(r.before)} <span className="muted">n={r.beforeN}</span></td>
                  <td>{r.beforeN < 5 ? <span className="chip">need more pours</span> : drifting ? <span className="chip warn">drifting {fmtSigned(shift, 2)} mL</span> : <span className="chip ok">steady</span>}</td></tr>
              );
            })}</tbody>
          </table></div>
          {ok(d) && d.rows.length === 0 && <p className="muted">No pours in Tiger yet.</p>}
        </div>
      </div>

      <div className="panel">
        <p className="label">Pour quality per hour · from the continuous aggregate <code>lw_pour_quality_1h</code></p>
        <div className="tbl"><table>
          <thead><tr><th>Hour</th><th>Tube</th><th className="num">Pours</th><th className="num">Mean error</th><th className="num">MAE</th><th className="num">In band</th></tr></thead>
          <tbody>{ok(q) && [...q.rows].reverse().slice(0, 24).map((r) => (
            <tr key={r.t + r.tube}><td className="mono">{new Date(r.t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit" })}</td><td><span className="dot" style={{ background: DYE[r.tube] }} /> {r.tube}</td>
              <td className="num">{r.n}</td><td className="num">{fmtSigned(r.meanErr)}</td><td className="num">{r.mae.toFixed(3)}</td><td className="num">{Math.round((r.inBand / r.n) * 100)}%</td></tr>
          ))}</tbody>
        </table></div>
        {ok(q) && q.rows.length === 0 && <p className="muted">Nothing in the last 48 hours.</p>}
      </div>

      <div className="panel">
        <div className="between">
          <div>
            <p className="label">Arm tuning from Tiger data · B2b test (grab vial B, carry, return)</p>
            <p className="muted" style={{ fontSize: "0.875rem", marginTop: 4, maxWidth: "70ch" }}>Gravity and friction feedforward fitted from the arm’s 100 Hz trace, plus a smoothed grab, cut J2’s tracking error from 2.16° to 0.28° RMS. J3 and J4 still sag about 2°.</p>
          </div>
          <div className="row">
            {Object.entries(METRICS).map(([k, v]) => <button key={k} className={`btn small ${metric === k ? "primary" : ""}`} onClick={() => setMetric(k)}>{v}</button>)}
          </div>
        </div>
        {rows.length > 0 && <div style={{ marginTop: 12 }}><CompareBars rows={rows} unit={unit} /></div>}
        {ok(a) && <p className="cap">Measured by the team on 2026-10-24. Baseline {a.baselineRun}, improved {a.improvedRun}. Stored in <code>lw_arm_tuning</code>.</p>}
      </div>
    </div>
  );
}
