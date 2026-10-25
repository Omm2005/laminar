import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAction } from "convex/react";
import { api } from "../convex/_generated/api";
import Arm3D, { type Telem } from "./robot/Arm3D";
import { ago } from "./ui";

type Runs = Extract<Awaited<ReturnType<ReturnType<typeof useAction<typeof api.tiger.armRuns>>>>, { available: true }>;
type Detail = Extract<Awaited<ReturnType<ReturnType<typeof useAction<typeof api.tiger.armRunDetail>>>>, { found: true }>;
type Pt = Detail["trace"][number];

const fmt = (x: number, d = 2) => (Number.isFinite(x) ? x.toFixed(d) : "—");

/** Two-panel trace chart like the team's Runs page: commanded vs measured, then tracking error + velocity. */
function TraceChart({ pts, joint }: { pts: Pt[]; joint: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const W = 1000, H1 = 230, H2 = 170, L = 52, R = 52, T = 14, B = 26;
  if (pts.length < 2) return <p className="muted">No trace samples for J{joint}.</p>;
  const s0 = pts[0].s, s1 = pts.at(-1)!.s;
  const x = (s: number) => L + ((s - s0) / (s1 - s0 || 1)) * (W - L - R);
  const ext = (vals: number[], pad = 0.08) => { const lo = Math.min(...vals), hi = Math.max(...vals), p = (hi - lo || 1) * pad; return [lo - p, hi + p] as const; };
  const [a1, b1] = ext(pts.flatMap((p) => [p.cmd, p.pos]));
  const eMax = Math.max(0.1, ...pts.map((p) => Math.abs(p.err))) * 1.1;
  const vMax = Math.max(1, ...pts.map((p) => Math.abs(p.vel))) * 1.1;
  const y1 = (v: number) => T + (1 - (v - a1) / (b1 - a1)) * (H1 - T - B);
  const ye = (v: number) => T + (1 - (v + eMax) / (2 * eMax)) * (H2 - T - B);
  const yv = (v: number) => T + (1 - (v + vMax) / (2 * vMax)) * (H2 - T - B);
  const line = (f: (p: Pt) => number, y: (v: number) => number) => pts.map((p) => `${x(p.s).toFixed(1)},${y(f(p)).toFixed(1)}`).join(" ");
  const ticks = (lo: number, hi: number, n = 4) => Array.from({ length: n + 1 }, (_, i) => lo + ((hi - lo) * i) / n);
  const xt = ticks(s0, s1, 4);
  const h = hover !== null ? pts[hover] : null;
  const onMove = (e: React.PointerEvent) => {
    const r = svg.current!.getBoundingClientRect();
    const sx = ((e.clientX - r.left) / r.width) * W;
    const s = s0 + ((sx - L) / (W - L - R)) * (s1 - s0);
    let best = 0;
    for (let i = 1; i < pts.length; i++) if (Math.abs(pts[i].s - s) < Math.abs(pts[best].s - s)) best = i;
    setHover(best);
  };
  const Tip = ({ y, rows }: { y: number; rows: [string, string, string][] }) => {
    if (!h) return null;
    const left = x(h.s) > W - 260;
    const bx = left ? x(h.s) - 232 : x(h.s) + 12;
    return (
      <g pointerEvents="none">
        <rect x={bx} y={y} width="220" height={22 + rows.length * 20} rx="8" fill="var(--surface-2)" stroke="var(--rule)" />
        <text x={bx + 12} y={y + 18} fontSize="12" fill="var(--ink)">{fmt(h.s, 1)} s</text>
        {rows.map(([k, v, c], i) => <text key={k} x={bx + 12} y={y + 38 + i * 20} fontSize="12" fill={c}>{k}: {v}</text>)}
      </g>
    );
  };
  return (
    <svg ref={svg} viewBox={`0 0 ${W} ${H1 + H2 + 30}`} style={{ width: "100%", height: "auto", display: "block", touchAction: "none" }}
      onPointerMove={onMove} onPointerLeave={() => setHover(null)} role="img" aria-label={`J${joint} commanded versus measured angle, tracking error and velocity`}>
      {/* panel 1 */}
      {ticks(a1, b1).map((v) => <g key={`a${v}`}><line x1={L} x2={W - R} y1={y1(v)} y2={y1(v)} stroke="var(--rule)" /><text x={L - 8} y={y1(v) + 4} fontSize="11" textAnchor="end" fill="var(--muted)">{fmt(v, 0)}°</text></g>)}
      {xt.map((s) => <text key={`x1${s}`} x={x(s)} y={H1 - 6} fontSize="11" textAnchor="middle" fill="var(--muted)">{fmt(s, 1)} s</text>)}
      <polyline points={line((p) => p.cmd, y1)} fill="none" stroke="var(--ink)" strokeWidth="1.4" strokeDasharray="5 4" opacity="0.7" />
      <polyline points={line((p) => p.pos, y1)} fill="none" stroke="#7dd3fc" strokeWidth="2" />
      <text x={L} y={T - 2} fontSize="11" fill="var(--muted)">- - commanded</text>
      <text x={L + 100} y={T - 2} fontSize="11" fill="#7dd3fc">— measured</text>
      {/* panel 2 */}
      <g transform={`translate(0 ${H1 + 30})`}>
        {[-eMax, -eMax / 2, 0, eMax / 2, eMax].map((v) => <g key={`e${v}`}><line x1={L} x2={W - R} y1={ye(v)} y2={ye(v)} stroke="var(--rule)" opacity={v === 0 ? 1 : 0.6} /><text x={L - 8} y={ye(v) + 4} fontSize="11" textAnchor="end" fill="#e8c257">{fmt(v, 1)}°</text></g>)}
        {[-vMax, 0, vMax].map((v) => <text key={`v${v}`} x={W - R + 8} y={yv(v) + 4} fontSize="11" fill="#8fdc7a">{fmt(v, 0)}</text>)}
        {xt.map((s) => <text key={`x2${s}`} x={x(s)} y={H2 - 6} fontSize="11" textAnchor="middle" fill="var(--muted)">{fmt(s, 1)} s</text>)}
        <polyline points={line((p) => p.vel, yv)} fill="none" stroke="#8fdc7a" strokeWidth="1.2" opacity="0.85" />
        <polyline points={line((p) => p.err, ye)} fill="none" stroke="#e8c257" strokeWidth="1.5" />
        <text x={L} y={T - 2} fontSize="11" fill="#e8c257">— tracking error °</text>
        <text x={L + 130} y={T - 2} fontSize="11" fill="#8fdc7a">— velocity °/s</text>
        {h && <>
          <line x1={x(h.s)} x2={x(h.s)} y1={T} y2={H2 - B} stroke="var(--ink)" opacity="0.5" />
          <circle cx={x(h.s)} cy={ye(h.err)} r="4.5" fill="#e8c257" />
          <circle cx={x(h.s)} cy={yv(h.vel)} r="4.5" fill="#8fdc7a" />
          <Tip y={T + 4} rows={[["tracking error °", fmt(h.err, 3), "#e8c257"], ["velocity °/s", fmt(h.vel, 2), "#8fdc7a"]]} />
        </>}
      </g>
      {h && <>
        <line x1={x(h.s)} x2={x(h.s)} y1={T} y2={H1 - B} stroke="var(--ink)" opacity="0.5" />
        <circle cx={x(h.s)} cy={y1(h.pos)} r="4.5" fill="#7dd3fc" />
        <Tip y={T + 6} rows={[["commanded", fmt(h.cmd, 3), "var(--ink)"], ["measured", fmt(h.pos, 3), "#7dd3fc"]]} />
      </>}
    </svg>
  );
}

/** Replays a run's measured joints from Tiger on the 3D arm. */
function Replay({ pts }: { pts: Pt[] }) {
  const [samples, setSamples] = useState<Telem[]>([]);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(4);
  const frames = useMemo(() => {
    const by = new Map<number, number[]>();
    for (const p of pts) { const row = by.get(p.s) ?? [0, 0, 0, 0, 0, 0]; row[p.j - 1] = p.pos; by.set(p.s, row); }
    return [...by.entries()].sort((a, b) => a[0] - b[0]);
  }, [pts]);
  const start = useCallback(() => {
    if (!frames.length) return;
    const t0 = Date.now() + 300, s0 = frames[0][0];
    setSamples(frames.map(([s, joints]) => ({ t: t0 + ((s - s0) * 1000) / speed, joints })));
    setPlaying(true);
  }, [frames, speed]);
  useEffect(() => { if (frames.length) setSamples([{ t: Date.now(), joints: frames[0][1] }]); setPlaying(false); }, [frames]);
  const dur = frames.length ? (frames.at(-1)![0] - frames[0][0]) / speed : 0;
  useEffect(() => { if (!playing) return; const t = setTimeout(() => setPlaying(false), dur * 1000 + 600); return () => clearTimeout(t); }, [playing, dur]);
  return (
    <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
      <div style={{ height: 340, position: "relative", background: "#0f1618" }}>
        <Arm3D samples={samples} status={playing ? "running" : "idle"} gripperDeg={30} cameraPreset="wide" />
        <span className="arm-tag mono">{playing ? `replaying at ${speed}× · measured joints from Tiger` : "measured joints from Tiger · press replay"}</span>
      </div>
      <div className="between" style={{ padding: "12px 16px" }}>
        <div className="row">
          {[1, 4, 8].map((x) => <button key={x} className={`btn small ${speed === x ? "primary" : ""}`} onClick={() => setSpeed(x)}>{x}×</button>)}
        </div>
        <button className="btn primary small" onClick={start} disabled={!frames.length}>{playing ? "Restart replay" : "Replay this run"}</button>
      </div>
    </div>
  );
}

export default function ArmRuns() {
  const list = useAction(api.tiger.armRuns);
  const detail = useAction(api.tiger.armRunDetail);
  const [runs, setRuns] = useState<Runs | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [d, setD] = useState<Detail | null>(null);
  const [joint, setJoint] = useState(1);
  const [state, setState] = useState<"loading" | "ok" | "none" | "error">("loading");
  useEffect(() => {
    list({ limit: 20 }).then((r) => {
      if (!("available" in r) || !r.available) { setState("none"); return; }
      setRuns(r); setState("ok"); setSel((s) => s ?? r.runs[0]?.runId ?? null);
    }).catch(() => setState("error"));
  }, [list]);
  useEffect(() => {
    if (!sel) return;
    setD(null);
    let alive = true, timer: ReturnType<typeof setTimeout>;
    const load = () => detail({ runId: sel }).then((r) => {
      if (!alive || !("found" in r) || !r.found) return;
      setD(r);
      // a run the arm is still doing: its 100 Hz trace keeps landing in Tiger, so keep reading it
      if (r.status === "running") timer = setTimeout(load, 3000);
    }).catch(() => setState("error"));
    void load();
    return () => { alive = false; clearTimeout(timer); };
  }, [sel, detail]);
  // new runs appear in the list while the page is open
  useEffect(() => {
    const t = setInterval(() => { list({ limit: 20 }).then((r) => { if ("available" in r && r.available) setRuns(r); }).catch(() => {}); }, 15000);
    return () => clearInterval(t);
  }, [list]);
  const pts = useMemo(() => d?.trace.filter((p) => p.j === joint) ?? [], [d, joint]);
  const worst = d ? [...d.metrics].sort((a, b) => b.rmsErr - a.rmsErr)[0] : null;

  if (state === "loading") return <p className="muted">Loading arm runs from Tiger…</p>;
  if (state === "none") return <p className="notice">No arm runs in this Tiger service yet. They appear once <code>software/laminar/tiger.py</code> logs a run.</p>;
  if (state === "error") return <p className="notice bad">Couldn’t read arm runs from Tiger.</p>;

  return (
    <div className="stack" style={{ gap: 18 }}>
      <div className="run-grid" style={{ gridTemplateColumns: "minmax(0, 0.8fr) minmax(0, 1.2fr)" }}>
        <div className="panel">
          <div className="between"><span className="label">Real arm runs · arm_runs</span><span className="chip">{runs?.runs.length} runs · {runs?.queryMs} ms</span></div>
          <div style={{ marginTop: 8, maxHeight: 420, overflowY: "auto" }}>
            {runs?.runs.map((r) => (
              <button key={r.runId} className="list-btn" aria-current={r.runId === sel} onClick={() => setSel(r.runId)}>
                <span style={{ minWidth: 0 }}>
                  <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.name ?? r.runId}</span>
                  <span className="muted" style={{ fontSize: "0.75rem" }}>{ago(r.started)} · {Math.round(((r.ended ?? r.started) - r.started) / 1000)} s</span>
                </span>
                <span className="row" style={{ gap: 6 }}>
                  <span className={`chip ${r.status === "done" ? "ok" : r.status === "failed" ? "bad" : r.status === "running" ? "live" : ""}`}>{r.status}</span>
                </span>
              </button>
            ))}
          </div>
        </div>
        {d ? <Replay pts={d.trace} /> : <div className="panel muted">Loading run…</div>}
      </div>

      {d && (
        <>
          <div className="panel">
            <div className="between" style={{ marginBottom: 10 }}>
              <div style={{ minWidth: 0 }}>
                <span className="label">J{joint}: commanded vs measured · {d.name ?? d.runId}</span>
              </div>
              <div className="row" style={{ gap: 6 }}>
                {[1, 2, 3, 4, 5, 6].map((j) => <button key={j} className={`btn small ${j === joint ? "primary" : ""}`} onClick={() => setJoint(j)}>J{j}</button>)}
              </div>
            </div>
            <TraceChart pts={pts} joint={joint} />
            <p className="cap">{pts.length} points · {d.bucketMs} ms buckets from the arm trace · J{joint} {d.rawSamples[joint]?.toLocaleString() ?? "—"} raw samples at 100 Hz · read from Tiger in {d.queryMs} ms via <code>run_trace()</code></p>
          </div>

          <div className="panel">
            <div className="between"><span className="label">Per-joint smoothness · run_smoothness()</span>{worst && <span className="chip warn">worst: J{worst.joint}, {fmt(worst.rmsErr)}° RMS</span>}</div>
            <div className="tbl" style={{ marginTop: 8 }}><table>
              <thead><tr><th>Joint</th><th className="num">RMS error °</th><th className="num">Max error °</th><th className="num">Peak speed °/s</th><th className="num">RMS jerk °/s³</th><th className="num">Stick-slip</th><th className="num">Peak torque N·m</th></tr></thead>
              <tbody>{d.metrics.map((m) => (
                <tr key={m.joint} style={{ cursor: "pointer" }} onClick={() => setJoint(m.joint)}>
                  <td><b style={{ color: m.joint === joint ? "var(--accent)" : undefined }}>J{m.joint}</b></td>
                  <td className="num" style={{ color: m.rmsErr > 1.5 ? "var(--warn)" : undefined }}>{fmt(m.rmsErr)}</td>
                  <td className="num">{fmt(m.maxErr)}</td><td className="num">{fmt(m.peakVel, 0)}</td>
                  <td className="num">{m.rmsJerk === null ? "—" : Math.round(m.rmsJerk).toLocaleString()}</td>
                  <td className="num">{m.stallFrac === null ? "—" : `${(m.stallFrac * 100).toFixed(1)}%`}</td>
                  <td className="num">{fmt(m.peakTorque)}</td>
                </tr>
              ))}</tbody>
            </table></div>
            <p className="cap">Stick-slip is the share of time the command moves faster than 2°/s while the joint moves slower than 0.5°/s. J3 and J4 sit about 2° low: steady sag the gravity model doesn’t explain yet.</p>
          </div>
        </>
      )}
    </div>
  );
}
