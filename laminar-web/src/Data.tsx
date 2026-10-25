import { useCallback, useEffect, useMemo, useState } from "react";
import { useAction, useQuery } from "convex/react";
import { api } from "../convex/_generated/api";
import { CompareBars, DYE, ago, clock, fmtSigned } from "./ui";
import ArmRuns from "./ArmRuns";

type Res<T> = T | undefined;
const ok = <T extends { configured: boolean }>(r: T | undefined): r is Extract<T, { configured: true }> => !!r && r.configured;
const METRICS: Record<string, string> = { rms_error: "RMS tracking error", max_error: "Max tracking error", peak_acc: "Peak acceleration", rms_jerk: "RMS jerk" };

const SQL_SAMPLES = {
  overview: `SELECT (SELECT count(*) FROM lw_pours)       AS pours,
       (SELECT count(*) FROM lw_events)      AS events,
       (SELECT count(*) FROM lw_pours
          WHERE ts > now() - interval '1 hour') AS pours_1h,
       (SELECT max(ts) FROM lw_pours)        AS last_pour;`,
  quality: `SELECT bucket, tube,
       sum(n)::int                     AS n,
       sum(mean_err * n) / sum(n)      AS mean_err,
       sum(mae * n) / sum(n)           AS mae,
       sum(in_band * n) / sum(n)       AS in_band
FROM   lw_pour_quality_1h              -- continuous aggregate (1 h buckets)
WHERE  bucket > now() - make_interval(hours => $1)
GROUP BY bucket, tube
ORDER BY bucket DESC, tube;`,
  drift: `WITH ranked AS (
  SELECT tube, error_ml,
         row_number() OVER (PARTITION BY tube ORDER BY ts DESC) AS k
  FROM   lw_pours
)
SELECT tube,
       avg(error_ml) FILTER (WHERE k <=  10) AS recent_mean,
       count(*)      FILTER (WHERE k <=  10) AS recent_n,
       avg(error_ml) FILTER (WHERE k >   10) AS before_mean,
       count(*)      FILTER (WHERE k >   10) AS before_n
FROM   ranked
GROUP BY tube
ORDER BY tube;`,
};

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
  const [sql, setSql] = useState<keyof typeof SQL_SAMPLES>("overview");
  const [tube, setTube] = useState<"A" | "B" | "C" | "D">("A");

  const telem = useQuery(api.robot.recent);
  const qc = useQuery(api.qc.recent, { limit: 120 });

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

  const latest = telem?.at(-1);
  const joints = latest?.joints ?? [];

  const rows = ok(a) ? a.rows.filter((r) => r.metric === metric) : [];
  const unit = rows[0]?.unit ?? "";

  const traces = useMemo(() => (qc?.pours ?? []).filter((p) => p.tube === tube).slice(-24), [qc, tube]);

  return (
    <div className="stack" style={{ paddingTop: 20 }}>
      {/* ingest strip */}
      <section className="ingest-strip">
        <div className="lead">
          <span className="label">Time series · Tiger Data</span>
          <h3>Every joint angle and every pour, as a hypertable.</h3>
          <span className="mono">lw_pours · lw_events · 100 Hz joints · 7-day compression</span>
        </div>
        <div>
          <span className="label">Rows today</span>
          <div className="fig">{ok(o) ? (o.pours + o.events).toLocaleString() : "…"}</div>
          <span className="sub">pours + events</span>
        </div>
        <div>
          <span className="label">Ingest Hz</span>
          <div className="fig">{ok(o) ? (o.pours1h / 3600).toFixed(2) : "…"}</div>
          <span className="sub">pours per second · last hour</span>
        </div>
        <div>
          <span className="label">Query p95 ms</span>
          <div className="fig">{ok(o) ? o.queryMs : "…"}</div>
          <span className="sub">Convex action → Tiger</span>
        </div>
        <div>
          <span className="label">Pours stored</span>
          <div className="fig">{ok(o) ? o.pours.toLocaleString() : "…"}</div>
          <span className="sub">{ok(o) && o.lastPour ? `last pour ${ago(o.lastPour)}` : ""}</span>
        </div>
      </section>

      {o && !ok(o) && <p className="notice warn">Tiger Data isn't connected. Set it with <code>npx convex env set TIGER_DATABASE_URL "postgres://…"</code>; the console keeps working without it.</p>}
      {err && <p className="notice bad">Couldn't reach Tiger: {err}. Pours are still saved in Convex.</p>}

      {/* pour traces */}
      <section className="card">
        <div className="between" style={{ marginBottom: 10 }}>
          <span className="label">Pour traces · delivered mL vs time · last 24</span>
          <div className="chips">
            {(["A", "B", "C", "D"] as const).map((t) => (
              <button key={t} type="button" className="chip filter" aria-pressed={tube === t} onClick={() => setTube(t)}>
                <span className="dot" style={{ background: DYE[t] }} /> {t}
              </button>
            ))}
          </div>
        </div>
        <PourTraces pours={traces} />
        <div className="row" style={{ justifyContent: "flex-end", marginTop: 10 }}>
          <span className="mono" style={{ fontSize: 11.5, color: "var(--ink-3)" }}>SELECT time_bucket('1 min', ts), tube, poured_ml FROM lw_pours WHERE tube = $1 ORDER BY ts DESC LIMIT 24;</span>
        </div>
      </section>

      {/* joints row + sql night */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 20 }}>
        <section className="card">
          <div className="between" style={{ marginBottom: 10 }}>
            <span className="label">Joints · latest row</span>
            <span className="chip">
              {latest ? <>live · {latest.source} · {clock(latest.t)}</> : "waiting on telemetry"}
            </span>
          </div>
          <div className="joints-grid">
            {[1, 2, 3, 4, 5, 6].map((j) => (
              <div key={j} className={j === 6 ? "j6" : ""}>
                <span className="label">J{j}{j === 6 ? " roll" : ""}</span>
                <span className="val">{joints[j - 1] != null ? joints[j - 1].toFixed(1) + "°" : "—"}</span>
              </div>
            ))}
            <div>
              <span className="label">Grip</span>
              <span className="val">{latest?.gripperDeg != null ? latest.gripperDeg.toFixed(0) + "°" : "—"}</span>
            </div>
          </div>
          <p className="mono" style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 10 }}>
            CAN · {latest ? "connected" : "offline"} · station cam {ok(o) ? `~${(o.pours1h / 3600).toFixed(1)} Hz` : "—"} · wrist cam {ok(o) ? "–" : "—"} fps · OM1 wells {qc?.pours?.length ?? 0}
          </p>
        </section>
        <section className="card night">
          <span className="label" style={{ color: "var(--mint)", display: "block", marginBottom: 6 }}>Queries the console runs</span>
          <div className="chips" style={{ margin: "6px 0 10px" }}>
            {(Object.keys(SQL_SAMPLES) as (keyof typeof SQL_SAMPLES)[]).map((k) => (
              <button key={k} type="button" className="chip filter" aria-pressed={sql === k}
                style={{ color: sql === k ? "var(--night)" : "var(--mint)", background: sql === k ? "var(--mint)" : "transparent", borderColor: "var(--mint)" }}
                onClick={() => setSql(k)}>
                {k}
              </button>
            ))}
          </div>
          <pre className="sql-pre">{SQL_SAMPLES[sql]}</pre>
        </section>
      </div>

      {/* existing hypertables / drift / quality / arm tuning / arm runs */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 20 }}>
        <section className="card">
          <span className="label" style={{ marginBottom: 10, display: "block" }}>Hypertables</span>
          <div className="tbl"><table>
            <thead><tr><th scope="col">Table</th><th scope="col" className="num">Chunks</th><th scope="col" className="num">Size</th><th scope="col">Compression</th></tr></thead>
            <tbody>{ok(o) && o.tables.map((t) => (
              <tr key={t.name}><td><code>{t.name}</code></td><td className="num">{t.chunks}</td><td className="num">{(t.bytes / 1024).toFixed(0)} KB</td><td>{t.compression ? "after 7 days" : "off"}</td></tr>
            ))}</tbody>
          </table></div>
          <span className="label" style={{ marginTop: 14, display: "block" }}>Background jobs</span>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18, fontSize: 14 }}>{ok(o) && o.policies.map((p) => <li key={p.name}>{p.name} · every {p.every}</li>)}</ul>
        </section>
        <section className="card">
          <span className="label" style={{ marginBottom: 10, display: "block" }}>Drift check · last 10 vs earlier · per tube</span>
          <div className="tbl"><table>
            <thead><tr><th scope="col">Tube</th><th scope="col" className="num">Recent mean</th><th scope="col" className="num">Earlier mean</th><th scope="col">Verdict</th></tr></thead>
            <tbody>{ok(d) && d.rows.map((r) => {
              const shift = r.recent !== null && r.before !== null ? r.recent - r.before : null;
              const drifting = shift !== null && Math.abs(shift) > 0.03 && r.beforeN >= 5;
              return (
                <tr key={r.tube}>
                  <td><span className="dot" style={{ background: DYE[r.tube] }} /> {r.tube}</td>
                  <td className="num">{fmtSigned(r.recent)} <span className="muted">n={r.recentN}</span></td>
                  <td className="num">{fmtSigned(r.before)} <span className="muted">n={r.beforeN}</span></td>
                  <td>{r.beforeN < 5 ? <span className="chip">need more pours</span> : drifting ? <span className="chip warn">drifting {fmtSigned(shift, 2)} mL</span> : <span className="chip ok">steady</span>}</td>
                </tr>
              );
            })}</tbody>
          </table></div>
          {ok(d) && d.rows.length === 0 && <p className="muted">No pours in Tiger yet.</p>}
        </section>
      </div>

      <section className="card">
        <span className="label" style={{ marginBottom: 10, display: "block" }}>Pour quality per hour · from <code>lw_pour_quality_1h</code></span>
        <div className="tbl"><table>
          <thead><tr><th scope="col">Hour</th><th scope="col">Tube</th><th scope="col" className="num">Pours</th><th scope="col" className="num">Mean error</th><th scope="col" className="num">MAE</th><th scope="col" className="num">In band</th></tr></thead>
          <tbody>{ok(q) && [...q.rows].reverse().slice(0, 24).map((r) => (
            <tr key={r.t + r.tube}>
              <td className="mono" style={{ fontSize: 12.5 }}>{new Date(r.t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit" })}</td>
              <td><span className="dot" style={{ background: DYE[r.tube] }} /> {r.tube}</td>
              <td className="num">{r.n}</td>
              <td className="num">{fmtSigned(r.meanErr)}</td>
              <td className="num">{r.mae.toFixed(3)}</td>
              <td className="num">{Math.round((r.inBand / r.n) * 100)}%</td>
            </tr>
          ))}</tbody>
        </table></div>
        {ok(q) && q.rows.length === 0 && <p className="muted">Nothing in the last 48 hours.</p>}
      </section>

      <section className="card">
        <div className="between">
          <div>
            <span className="label">Arm tuning · B2b test (grab vial B, carry, return)</span>
            <p className="muted" style={{ fontSize: 13.5, marginTop: 6, maxWidth: "70ch" }}>Gravity and friction feedforward fitted from the arm's 100 Hz trace, plus a smoothed grab, cut J2's tracking error from 2.16° to 0.28° RMS. J3 and J4 still sag about 2°.</p>
          </div>
          <div className="chips">
            {Object.entries(METRICS).map(([k, v]) => <button key={k} type="button" className="chip filter" aria-pressed={metric === k} onClick={() => setMetric(k)}>{v}</button>)}
          </div>
        </div>
        {rows.length > 0 && <div style={{ marginTop: 12 }}><CompareBars rows={rows} unit={unit} /></div>}
        {ok(a) && <p className="cap" style={{ marginTop: 10 }}>Measured by the team on 2026-10-24. Baseline {a.baselineRun}, improved {a.improvedRun}. Stored in <code>lw_arm_tuning</code>.</p>}
      </section>

      <section className="card">
        <span className="label" style={{ marginBottom: 10, display: "block" }}>Arm runs · 100 Hz traces from Tiger</span>
        <ArmRuns />
      </section>
    </div>
  );
}

function PourTraces({ pours }: { pours: { t: number; pouredMl: number; targetMl: number }[] }) {
  const W = 760, H = 220, L = 46, R = 16, T = 14, B = 26;
  if (pours.length === 0) return <p className="muted" style={{ padding: "48px 0", textAlign: "center" }}>No pours for this tube yet.</p>;
  const ts = pours.map((p) => p.t);
  const vs = pours.map((p) => p.pouredMl);
  const targets = pours.map((p) => p.targetMl);
  const t0 = Math.min(...ts), t1 = Math.max(...ts);
  const vMin = 0, vMax = Math.max(...vs, ...targets) * 1.15 || 1;
  const x = (t: number) => L + ((t - t0) / (t1 - t0 || 1)) * (W - L - R);
  const y = (v: number) => T + (1 - (v - vMin) / (vMax - vMin)) * (H - T - B);
  // target band: an envelope at target ±10%
  const bandUpper = pours.map((p) => y(p.targetMl * 1.1));
  const bandLower = pours.map((p) => y(p.targetMl * 0.9));
  const bandPath = pours.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${bandUpper[i].toFixed(1)}`).join(" ") +
    " " + pours.map((p, i) => `L${x(p.t).toFixed(1)},${bandLower[pours.length - 1 - i >= 0 ? pours.length - 1 - i : 0].toFixed(1)}`).reverse().join(" ") + " Z";
  const linePath = pours.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.pouredMl).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Delivered mL vs time for ${pours.length} pours`} style={{ width: "100%", height: "auto", display: "block" }}>
      <path d={bandPath} fill="var(--jade)" opacity="0.16" />
      {[0, 1, 2].map((i) => {
        const v = vMin + ((vMax - vMin) * i) / 2;
        return <g key={i}>
          <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--rule-2)" />
          <text x={L - 8} y={y(v) + 4} textAnchor="end" fontSize="11" fill="var(--ink-3)" fontFamily="var(--f-mono)">{v.toFixed(1)}</text>
        </g>;
      })}
      <path d={linePath} fill="none" stroke="var(--reagent)" strokeWidth="1.5" opacity="0.6" />
      {pours.map((p, i) => (
        <circle key={i} cx={x(p.t)} cy={y(p.pouredMl)} r={i === pours.length - 1 ? 5.5 : 3.5}
          fill={i === pours.length - 1 ? "var(--mint)" : "var(--reagent)"} opacity={i === pours.length - 1 ? 1 : 0.4 + (i / pours.length) * 0.6}>
          <title>{`${p.pouredMl.toFixed(2)} mL of ${p.targetMl.toFixed(2)} · ${clock(p.t)}`}</title>
        </circle>
      ))}
    </svg>
  );
}
