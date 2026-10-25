import { useEffect, useMemo, useState } from "react";
import { useAction } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ReferenceLine, ResponsiveContainer, CartesianGrid, Legend,
} from "recharts";
import { api } from "../../convex/_generated/api";

type RunRow = FunctionReturnType<typeof api.tiger.runs>[number];
type Detail = NonNullable<FunctionReturnType<typeof api.tiger.runDetail>>;
type Metric = Detail["metrics"][number];

const AXIS = "#7d869c";
const GRID = "#1f2531";
const CMD = "#e2e6ee";
const POS = "#7dd3fc";
const ERR = "#e8c257";
const VEL = "#68d391";

/** Poll an action every `ms` (or once when ms is null). Keeps the last good value through errors. */
function usePolled<T>(fn: () => Promise<T>, deps: unknown[], ms: number | null) {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const d = await fn();
        if (alive) { setData(d); setError(null); }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      }
      if (alive && ms) timer = setTimeout(tick, ms);
    };
    tick();
    return () => { alive = false; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, ms]);
  return { data, error };
}

const fmtBytes = (b: number) => (b > 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${(b / 1e3).toFixed(0)} kB`);
const fmtAgo = (t: number | null) => {
  if (!t) return "never";
  const s = (Date.now() - t) / 1000;
  return s < 60 ? `${s.toFixed(0)} s ago` : s < 3600 ? `${(s / 60).toFixed(0)} min ago` : `${(s / 3600).toFixed(1)} h ago`;
};
const fmtDur = (a: number, b: number | null) => `${(((b ?? Date.now()) - a) / 1000).toFixed(1)} s`;

/** Heuristic reading of one joint's numbers: where to look first when smoothing the trajectory. */
function hints(m: Metric): string[] {
  const out: string[] = [];
  if ((m.stallFrac ?? 0) > 0.15) out.push(`stick-slip: commanded to move but stalled ${((m.stallFrac ?? 0) * 100).toFixed(0)}% of the time — more velocity feedforward or a slower, longer ramp`);
  if (m.maxErr > 3) out.push(`following error up to ${m.maxErr.toFixed(1)}° — the command outruns the joint; lower peak velocity/acceleration or raise kp`);
  if ((m.rmsJerk ?? 0) > 20000) out.push(`jerky profile (RMS jerk ${(m.rmsJerk ?? 0).toFixed(0)}°/s³) — blend waypoints or use a minimum-jerk profile`);
  return out;
}

function MetricCell({ v, warn, bad, digits = 2, suffix = "" }: { v: number | null; warn: number; bad: number; digits?: number; suffix?: string }) {
  if (v == null) return <td className="mono num dim">–</td>;
  const color = v >= bad ? "var(--bad)" : v >= warn ? "var(--warn)" : undefined;
  return <td className="mono num" style={{ color }}>{v.toFixed(digits)}{suffix}</td>;
}

function StorePanel() {
  const run = useAction(api.tiger.overview);
  const { data: o, error } = usePolled(() => run({}), [], 5000);
  return (
    <section className="card">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <div className="card-title" style={{ margin: 0 }}>Tiger Data</div>
        <span className="small dim">
          {error ? <span style={{ color: "var(--bad)" }}>{error}</span>
            : o ? <>last event {fmtAgo(o.lastEvent)} · last trace {fmtAgo(o.lastTrace)}</> : "connecting…"}
        </span>
      </div>
      {o && (
        <div className="tiger-stats">
          <div><div className="tiger-big num">{o.runs}</div><div className="small dim">runs logged</div></div>
          <div><div className="tiger-big num">{o.events1h.toLocaleString()}</div><div className="small dim">events, last hour</div></div>
          <div><div className="tiger-big num">{o.trace1h.toLocaleString()}</div><div className="small dim">trace rows, last hour</div></div>
          {o.tables.map((t) => (
            <div key={t.name}>
              <div className="mono small">{t.name}</div>
              <div className="small dim num">{t.approxRows.toLocaleString()} rows · {fmtBytes(t.bytes)}</div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function RunList({ selected, onSelect }: { selected: string | null; onSelect: (id: string) => void }) {
  const run = useAction(api.tiger.runs);
  const { data, error } = usePolled<RunRow[]>(() => run({ limit: 30 }), [], 5000);
  useEffect(() => {
    if (!selected && data && data.length) onSelect(data[0].runId);
  }, [data, selected, onSelect]);
  return (
    <section className="card runs-list">
      <div className="card-title">Runs</div>
      {error && <div className="small" style={{ color: "var(--bad)" }}>{error}</div>}
      {data && data.length === 0 && (
        <div className="empty-text">
          No runs yet. Approve a job (run_job logs it), or wrap any command:
          <div className="mono small" style={{ marginTop: 8 }}>python -m laminar.tiger run -- python transfer.py vial_A well_3</div>
        </div>
      )}
      {(data ?? []).map((r) => (
        <button key={r.runId} className={`run-row ${r.runId === selected ? "run-row-on" : ""}`} onClick={() => onSelect(r.runId)}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="run-name">{r.name ?? r.runId}</span>
            <span className={`pill run-${r.status}`}>{r.status}</span>
          </div>
          <div className="small dim num">
            {new Date(r.started).toLocaleTimeString()} · {fmtDur(r.started, r.ended)}
            {r.worstJoint != null && r.worstRms != null && <> · worst J{r.worstJoint} {r.worstRms.toFixed(2)}° rms</>}
          </div>
        </button>
      ))}
    </section>
  );
}

function RunDetail({ runId }: { runId: string }) {
  const get = useAction(api.tiger.runDetail);
  // poll while the run is open; once it has ended, one more fetch and stop
  const [status, setStatus] = useState<string | null>(null);
  const live = status == null || status === "running";
  const { data, error } = usePolled<Detail | null>(async () => {
    const d = await get({ runId });
    setStatus(d?.status ?? "missing");
    return d;
  }, [runId], live ? 3000 : null);
  const [joint, setJoint] = useState(1);

  const series = useMemo(() => {
    if (!data) return [];
    const tr = data.trace;
    const out: { s: number; cmd: number; pos: number; err: number; vel: number }[] = [];
    for (let i = 0; i < tr.t.length; i++) {
      if (tr.joint[i] !== joint) continue;
      out.push({ s: +((tr.t[i] - data.started) / 1000).toFixed(2), cmd: tr.cmd[i], pos: tr.pos[i], err: tr.err[i], vel: tr.vel[i] });
    }
    return out;
  }, [data, joint]);

  const steps = useMemo(() => (data?.events ?? [])
    .filter((e) => e.event === "job.stepStart")
    .map((e) => ({ s: (e.t - data!.started) / 1000, name: String(e.data?.name ?? "") })), [data]);

  if (error && !data) return <section className="card"><div style={{ color: "var(--bad)" }}>{error}</div></section>;
  if (!data) return <section className="card"><div className="empty-text">Loading run…</div></section>;

  const m = data.metrics.find((x) => x.joint === joint);
  const allHints = data.metrics.flatMap((x) => hints(x).map((h) => `J${x.joint} ${h}`));

  return (
    <div className="runs-detail">
      <section className="card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
          <div>
            <div className="card-title" style={{ margin: 0 }}>{data.name ?? data.runId}</div>
            <div className="small dim mono">{data.runId} · {new Date(data.started).toLocaleString()} · {fmtDur(data.started, data.ended)}</div>
          </div>
          <span className={`pill run-${data.status}`}>{data.status}{live && data.status === "running" ? " · live" : ""}</span>
        </div>
        {data.metrics.length === 0 ? (
          <div className="empty-text">No arm trace in this run's window. Is arm_server (with arm_trace) running?</div>
        ) : (
          <table className="table" style={{ marginTop: 12 }}>
            <thead>
              <tr>
                <th>Joint</th><th title="RMS of commanded − measured">RMS err °</th><th>Max err °</th>
                <th>Peak vel °/s</th><th>Peak acc °/s²</th><th title="RMS jerk on 50 ms buckets">RMS jerk °/s³</th>
                <th title="time stalled while commanded to move">Stall</th><th>Peak τ N·m</th>
              </tr>
            </thead>
            <tbody>
              {data.metrics.map((x) => (
                <tr key={x.joint} className={x.joint === joint ? "run-joint-on" : ""} style={{ cursor: "pointer" }} onClick={() => setJoint(x.joint)}>
                  <td className="mono">J{x.joint}</td>
                  <MetricCell v={x.rmsErr} warn={1} bad={2} />
                  <MetricCell v={x.maxErr} warn={3} bad={6} />
                  <MetricCell v={x.peakVel} warn={1e9} bad={1e9} digits={1} />
                  <MetricCell v={x.peakAcc} warn={1e9} bad={1e9} digits={0} />
                  <MetricCell v={x.rmsJerk} warn={20000} bad={60000} digits={0} />
                  <MetricCell v={x.stallFrac == null ? null : x.stallFrac * 100} warn={15} bad={35} digits={0} suffix="%" />
                  <MetricCell v={x.peakTorque} warn={1e9} bad={1e9} />
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <div className="card-title" style={{ margin: 0 }}>J{joint}: commanded vs measured</div>
          <div className="chip-row">
            {[1, 2, 3, 4, 5, 6].map((j) => (
              <button key={j} className={`chip ${j === joint ? "chip-on" : ""}`} onClick={() => setJoint(j)}>J{j}</button>
            ))}
          </div>
        </div>
        <div style={{ height: 240, marginTop: 8 }}>
          <ResponsiveContainer>
            <LineChart data={series} margin={{ top: 8, right: 8, left: -8, bottom: 0 }} syncId="run">
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="s" type="number" domain={["dataMin", "dataMax"]} stroke={AXIS} tick={{ fontSize: 10 }} unit=" s" />
              <YAxis stroke={AXIS} tick={{ fontSize: 10 }} width={44} unit="°" domain={["auto", "auto"]} />
              <Tooltip contentStyle={{ background: "#141821", border: "1px solid #1f2531" }} labelFormatter={(s) => `${s} s`} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {steps.map((st, i) => <ReferenceLine key={i} x={st.s} stroke="#262c3b" label={{ value: st.name, fill: AXIS, fontSize: 9, position: "insideTopLeft" }} />)}
              <Line dataKey="cmd" name="commanded" stroke={CMD} strokeDasharray="4 3" dot={false} isAnimationActive={false} />
              <Line dataKey="pos" name="measured" stroke={POS} dot={false} strokeWidth={2} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div style={{ height: 180, marginTop: 8 }}>
          <ResponsiveContainer>
            <LineChart data={series} margin={{ top: 8, right: 8, left: -8, bottom: 0 }} syncId="run">
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="s" type="number" domain={["dataMin", "dataMax"]} stroke={AXIS} tick={{ fontSize: 10 }} unit=" s" />
              <YAxis yAxisId="e" stroke={ERR} tick={{ fontSize: 10 }} width={44} unit="°" />
              <YAxis yAxisId="v" orientation="right" stroke={VEL} tick={{ fontSize: 10 }} width={44} />
              <Tooltip contentStyle={{ background: "#141821", border: "1px solid #1f2531" }} labelFormatter={(s) => `${s} s`} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <ReferenceLine yAxisId="e" y={0} stroke={GRID} />
              <Line yAxisId="e" dataKey="err" name="tracking error °" stroke={ERR} dot={false} isAnimationActive={false} />
              <Line yAxisId="v" dataKey="vel" name="velocity °/s" stroke={VEL} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="small dim">
          {series.length} points · {data.bucketMs} ms buckets from the arm trace
          {m && <> · J{joint} {m.samples.toLocaleString()} raw samples</>}
        </div>
      </section>

      <section className="card">
        <div className="card-title">Where to smooth</div>
        {allHints.length === 0 ? (
          <div className="empty-text">{data.metrics.length ? "Every joint tracks within the thresholds for this run." : "Needs an arm trace."}</div>
        ) : (
          <ul className="run-hints">{allHints.map((h, i) => <li key={i}>{h}</li>)}</ul>
        )}
        <div className="small dim">Thresholds are rough starting points (stall &gt; 15%, max err &gt; 3°, RMS jerk &gt; 20k°/s³); compare runs before tuning.</div>
      </section>
    </div>
  );
}

export default function Runs() {
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <div className="runs-page">
      <StorePanel />
      <div className="runs-grid">
        <RunList selected={selected} onSelect={setSelected} />
        {selected ? <RunDetail runId={selected} /> : <section className="card"><div className="empty-text">Pick a run.</div></section>}
      </div>
    </div>
  );
}
