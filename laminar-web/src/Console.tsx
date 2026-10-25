import { useEffect, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../convex/_generated/api";
import type { Id } from "../convex/_generated/dataModel";
import { Bench, DYE, ErrorChart, Status, Via, ago, clock, fmtSigned, useName } from "./ui";
import Game from "./Game";
import Data from "./Data";
import Builder from "./Builder";
import LiveArm from "./robot/LiveArm";
import Voice from "./Voice";
import Cameras from "./Cameras";
import Chat from "./Chat";

const TABS = [
  ["run", "Run"], ["chat", "Assistant"], ["builder", "Protocol builder"], ["qc", "Run log & QC"], ["reports", "Reports"], ["audit", "Audit"], ["game", "Beat the robot"], ["data", "Tiger Data"],
] as const;
type Tab = (typeof TABS)[number][0];

const EXAMPLES = ["Run the assay on sample A", "Only test reagent C on two wells", "Pour 1.5 ml of A into well 2", "Pour some blue"];

function msg(e: unknown) {
  const m = (e as Error)?.message ?? String(e);
  return m.replace(/^.*Uncaught Error: /s, "").replace(/\s+at .*$/s, "").replace(/\[Request ID.*?\]\s*/, "").trim();
}

export default function Console() {
  const [tab, setTab] = useState<Tab>(() => {
    const q = new URLSearchParams(location.search).get("tab");
    if (q && TABS.some(([id]) => id === q)) return q as Tab;
    try { return (localStorage.getItem("laminar.tab") as Tab) || "run"; } catch { return "run"; }
  });
  useEffect(() => { try { localStorage.setItem("laminar.tab", tab); } catch { /* ignore */ } }, [tab]);
  const [name, setName] = useName();
  const ensure = useMutation(api.bench.ensure);
  useEffect(() => { void ensure({}); }, [ensure]);

  return (
    <main className="console">
      <div className="wrap">
        <div className="console-head">
          <div>
            <p className="label">Laminar console</p>
            <h1>Plan, approve and check every pour</h1>
          </div>
          <label className="who" htmlFor="who">
            <span className="label">You are</span>
            <input id="who" className="input" placeholder="Your name" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
        </div>
        <div className="tabs" role="tablist">
          {TABS.map(([id, label]) => (
            <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>
          ))}
        </div>
        {tab === "run" && <RunTab name={name} />}
        {tab === "chat" && <Chat name={name} />}
        {tab === "builder" && <Builder name={name} onSent={() => setTab("run")} />}
        {tab === "qc" && <QcTab />}
        {tab === "reports" && <ReportsTab />}
        {tab === "audit" && <AuditTab />}
        {tab === "game" && <Game name={name} />}
        {tab === "data" && <Data />}
      </div>
    </main>
  );
}

// ---------------------------------------------------------------- Run

function RunTab({ name }: { name: string }) {
  const bench = useQuery(api.bench.state);
  const runs = useQuery(api.runs.list, { limit: 12 });
  const [selected, setSelected] = useState<Id<"runs"> | null>(null);
  const current = selected ?? runs?.[0]?._id ?? null;
  const detail = useQuery(api.runs.get, current ? { id: current } : "skip");
  const submit = useMutation(api.runs.submit);
  const approve = useMutation(api.runs.approve);
  const cancel = useMutation(api.runs.cancel);
  const swap = useMutation(api.bench.swap);
  const resetRack = useMutation(api.bench.resetRack);
  const refill = useMutation(api.bench.refill);
  const emptyTray = useMutation(api.bench.emptyTray);
  const [text, setText] = useState(EXAMPLES[0]);
  const [error, setError] = useState("");
  const act = async (f: () => Promise<unknown>) => {
    setError("");
    try { await f(); } catch (e) { setError(msg(e)); }
  };

  const run = detail?.run;
  const step = run?.step ?? 0;
  const moving = run?.status === "running";
  const move = moving ? run?.moves?.[Math.max(0, step - 1)] : undefined;
  const holding = moving ? run?.moves?.slice(0, step).reverse().find((m) => m.startsWith("grab_") || m.startsWith("release_")) : undefined;
  const inHand = holding?.startsWith("grab_") ? holding.slice(5) : undefined;
  const swapped = bench?.tubes.some((t, i) => t.letter !== "ABCD"[i]);

  return (
    <div className="run-grid">
      <div className="stack" style={{ gap: 18 }}>
        <form className="panel stack" style={{ gap: 12 }} onSubmit={(e) => { e.preventDefault(); void act(async () => { const id = await submit({ request: text, requestedBy: name }); setSelected(id); }); }}>
          <label className="field" htmlFor="req">
            <span className="label">Ask for a run</span>
            <textarea id="req" className="input" value={text} onChange={(e) => setText(e.target.value)} rows={2} />
          </label>
          <div className="chips">
            {EXAMPLES.map((x) => <button type="button" key={x} className="chip" style={{ cursor: "pointer", background: "none" }} onClick={() => setText(x)}>{x}</button>)}
          </div>
          <div className="between">
            <span className="muted" style={{ fontSize: "0.8125rem" }}>Claude plans it, the checker verifies it, you approve it.</span>
            <button className="btn primary" disabled={!text.trim() || !name.trim()}>{name.trim() ? "Plan this run" : "Enter your name first"}</button>
          </div>
          {error && <p className="err" role="alert">{error}</p>}
        </form>

        <div className="panel">
          <Voice
            name={name}
            run={run}
            onRequest={async (spoken) => { setText(spoken); await act(async () => { const id = await submit({ request: spoken, requestedBy: name, via: "voice" }); setSelected(id); }); }}
            onApprove={() => act(() => approve({ id: run!._id, by: `${name} (voice)` }))}
            onCancel={(reason) => act(() => cancel({ id: run!._id, by: `${name} (voice)`, reason }))}
          />
        </div>

        {run && (
          <div className="panel stack" style={{ gap: 14 }}>
            <div className="between">
              <div>
                <p className="label">Run · {clock(run.createdAt)} · by {run.requestedBy}</p>
                <h3 style={{ fontSize: "var(--s-2)", marginTop: 4 }}>“{run.request}”</h3>
              </div>
              <span className="row" style={{ gap: 6 }}><Via v={run.via} /><Status s={run.status} /></span>
            </div>
            {run.status === "checking" && <p className="notice">Planning and checking…</p>}
            {run.plannerNote && <p className="notice"><span className="label">Read-back · {run.planner === "rules" ? "rule planner" : run.planner}</span><br />{run.plannerNote}</p>}
            {run.question && <p className="notice warn"><span className="label">Question back, nothing planned</span><br />{run.question}</p>}
            {!!run.errors?.length && (
              <div className="notice bad"><span className="label">Refused before motion</span>
                <ul>{run.errors.map((e) => <li key={e}>{e}</li>)}</ul>
              </div>
            )}
            {run.status === "awaiting_approval" && (
              <div className="row">
                <button className="btn primary" disabled={!name.trim()} onClick={() => act(() => approve({ id: run._id, by: name }))}>Approve {run.moves?.length} moves</button>
                <button className="btn" onClick={() => act(() => cancel({ id: run._id, by: name || "operator" }))}>Cancel</button>
              </div>
            )}
            {(moving || run.status === "approved") && (
              <button className="btn danger solid" onClick={() => act(() => cancel({ id: run._id, by: name || "operator", reason: "Stopped by operator" }))}>Stop</button>
            )}
            {run.abortReason && <p className="notice bad">Aborted: {run.abortReason}</p>}
            {run.result && <p className="notice ok"><span className="label">Measured readout</span><br />{run.result}</p>}
            {run.summary && <p className="notice"><span className="label">Claude’s note</span><br />{run.summary}</p>}
            {run.moves && (
              <ol className="moves" aria-label="Move list">
                {run.moves.map((m, i) => (
                  <li key={i} className={i < step - 1 || run.status === "done" ? "done" : moving && i === step - 1 ? "now" : ""}>
                    <span>{i + 1}</span><span>{m.startsWith("pour") ? <b>{m}</b> : m}</span>
                  </li>
                ))}
              </ol>
            )}
            {!!detail?.pours.length && (
              <div className="tbl">
                <table>
                  <thead><tr><th>Tube</th><th>Well</th><th className="num">Target</th><th className="num">Poured</th><th className="num">Error</th></tr></thead>
                  <tbody>
                    {detail.pours.map((p) => (
                      <tr key={p._id}>
                        <td><span className="dot" style={{ background: DYE[p.tube] }} /> {p.tube}</td><td>{p.well}</td>
                        <td className="num">{p.targetMl.toFixed(2)}</td><td className="num">{p.pouredMl.toFixed(2)}</td>
                        <td className="num" style={{ color: Math.abs(p.errorMl) > 0.1 * p.targetMl ? "var(--bad)" : undefined }}>{fmtSigned(p.errorMl, 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="stack" style={{ gap: 18 }}>
        <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
          <LiveArm height={360} />
        </div>
        <Cameras />
        <div className="panel">
          <div className="between" style={{ marginBottom: 6 }}>
            <span className="label">Bench · simulated until the arm is connected</span>
            {moving ? <span className="chip live">move {step} of {run?.moves?.length}</span> : <span className="chip">idle</span>}
          </div>
          {bench && <Bench tubes={bench.tubes} wells={bench.wells} move={move} holding={inHand} pouring={!!move?.startsWith("pour")} />}
          {swapped && <p className="notice warn" style={{ marginTop: 8 }}>The rack is out of order. Any run you ask for now will be refused until it’s fixed.</p>}
          <div className="row" style={{ marginTop: 12 }}>
            <button className="btn small" disabled={!!bench?.running} onClick={() => act(() => swap({ a: 3, b: 4, by: name }))}>Swap tubes C and D</button>
            <button className="btn small" disabled={!!bench?.running || !swapped} onClick={() => act(() => resetRack({ by: name }))}>Put rack in order</button>
            <button className="btn small" disabled={!!bench?.running} onClick={() => act(() => refill({ by: name }))}>Refill tubes</button>
            <button className="btn small" disabled={!!bench?.running} onClick={() => act(() => emptyTray({ by: name }))}>Empty the wells</button>
          </div>
          {bench && (
            <div className="tbl" style={{ marginTop: 12 }}>
              <table>
                <thead><tr><th>Slot</th><th>Tube</th><th>Stands for</th><th className="num">Volume</th></tr></thead>
                <tbody>
                  {bench.tubes.map((t) => (
                    <tr key={t._id}>
                      <td>{t.slot}</td><td><span className="dot" style={{ background: DYE[t.letter] }} /> {t.letter}</td><td>{t.stands}</td>
                      <td className="num" style={{ color: t.volumeMl < bench.lowMl ? "var(--warn)" : undefined }}>{t.volumeMl.toFixed(2)} / {t.fillLineMl.toFixed(1)} mL{t.volumeMl < bench.lowMl ? " · low" : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <div className="panel">
          <p className="label" style={{ marginBottom: 4 }}>Recent runs</p>
          {runs?.length === 0 && <p className="muted">No runs yet. Ask for the standard assay to start.</p>}
          {runs?.map((r) => (
            <button key={r._id} className="list-btn" aria-current={r._id === current} onClick={() => setSelected(r._id)}>
              <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.request} <span className="muted">· {r.requestedBy} · {ago(r.createdAt)}</span></span>
              <span className="row" style={{ gap: 6 }}><Via v={r.via} /><Status s={r.status} /></span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- QC

function QcTab() {
  const qc = useQuery(api.qc.recent, { limit: 120 });
  if (!qc) return <p className="muted">Loading…</p>;
  const o = qc.overall;
  return (
    <div className="stack">
      <div className="tiles">
        <div><span className="label">Pours</span><b>{o.n}</b><small>latest 120</small></div>
        <div><span className="label">Within ±10%</span><b>{o.inBandPct ?? "—"}{o.inBandPct != null && "%"}</b><small>goal: every pour</small></div>
        <div><span className="label">Mean error</span><b>{fmtSigned(o.meanErrorMl)}</b><small>mL, signed</small></div>
        <div><span className="label">Mean abs error</span><b>{o.maeMl?.toFixed(3) ?? "—"}</b><small>mL</small></div>
        <div><span className="label">Spread</span><b>{o.sdMl?.toFixed(3) ?? "—"}</b><small>mL, std dev</small></div>
        <div><span className="label">Runs finished</span><b>{qc.runSuccessPct ?? "—"}{qc.runSuccessPct != null && "%"}</b><small>done vs aborted</small></div>
        <div><span className="label">Plans refused</span><b>{qc.refusals}</b><small>by the checker</small></div>
      </div>
      <div className="panel">
        <div className="between" style={{ marginBottom: 6 }}>
          <span className="label">Pour error, % of target</span>
          <div className="chips">{(["A", "B", "C", "D"] as const).map((t) => <span key={t} className="chip"><span className="dot" style={{ background: DYE[t] }} /> {t}</span>)}</div>
        </div>
        {qc.pours.length ? <ErrorChart pours={qc.pours} /> : <p className="muted" style={{ padding: "40px 0" }}>No pours yet. Run the assay and each pour appears here as it lands.</p>}
      </div>
      <div className="grid2">
        <div className="panel">
          <p className="label">Per tube</p>
          <div className="tbl"><table>
            <thead><tr><th>Tube</th><th className="num">Pours</th><th className="num">Mean</th><th className="num">MAE</th><th className="num">In band</th></tr></thead>
            <tbody>{qc.perTube.map((t) => (
              <tr key={t.tube}><td><span className="dot" style={{ background: DYE[t.tube] }} /> {t.tube}</td><td className="num">{t.n}</td><td className="num">{fmtSigned(t.meanErrorMl)}</td><td className="num">{t.maeMl?.toFixed(3) ?? "—"}</td><td className="num">{t.inBandPct ?? "—"}{t.inBandPct != null && "%"}</td></tr>
            ))}</tbody>
          </table></div>
        </div>
        <div className="panel">
          <p className="label">Recent aborts</p>
          {qc.aborts.length === 0 ? <p className="muted" style={{ marginTop: 8 }}>None.</p> : (
            <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>{qc.aborts.map((a, i) => <li key={i}>{a.reason} <span className="muted">· {ago(a.t)}</span></li>)}</ul>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Reports

function ReportsTab() {
  const reports = useQuery(api.reports.list);
  const gen = useMutation(api.reports.generateNow);
  const [sel, setSel] = useState<string | null>(null);
  const r = reports?.find((x) => x._id === sel) ?? reports?.[0];
  return (
    <div className="run-grid">
      <div className="panel">
        <div className="between">
          <div>
            <p className="label">Daily QC report</p>
            <p className="muted" style={{ fontSize: "0.875rem", marginTop: 4 }}>Generated at 08:00 Los Angeles time from the previous 24 hours. Frozen once written.</p>
          </div>
          <button className="btn" onClick={() => gen({})}>Generate now</button>
        </div>
        <div style={{ marginTop: 10 }}>
          {reports?.length === 0 && <p className="muted">No reports yet.</p>}
          {reports?.map((x) => (
            <button key={x._id} className="list-btn" aria-current={x._id === r?._id} onClick={() => setSel(x._id)}>
              <span>{x.date} <span className="muted">· {x.trigger === "schedule" ? "scheduled" : "manual"} · {clock(x.generatedAt)}</span></span>
              <span className="muted num">{x.pours} pours</span>
            </button>
          ))}
        </div>
      </div>
      {r && (
        <div className="panel stack" style={{ gap: 16 }}>
          <div>
            <p className="label">{r.date} · {new Date(r.windowFrom).toLocaleString()} → {new Date(r.windowTo).toLocaleTimeString()}</p>
            <p className="notice" style={{ marginTop: 10 }}><span className="label">Recommendation</span><br />{r.recommendation}</p>
          </div>
          <div className="tiles">
            <div><span className="label">Runs</span><b>{r.runs}</b><small>{r.done} done · {r.aborted} aborted</small></div>
            <div><span className="label">Pours</span><b>{r.pours}</b></div>
            <div><span className="label">Within ±10%</span><b>{r.inBandPct ?? "—"}{r.inBandPct != null && "%"}</b></div>
            <div><span className="label">Mean error</span><b>{fmtSigned(r.meanErrorMl)}</b><small>mL</small></div>
          </div>
          {r.perTube.length > 0 && (
            <div className="tbl"><table>
              <thead><tr><th>Tube</th><th className="num">Pours</th><th className="num">Mean error</th><th className="num">MAE</th></tr></thead>
              <tbody>{r.perTube.map((t) => <tr key={t.tube}><td><span className="dot" style={{ background: DYE[t.tube] }} /> {t.tube}</td><td className="num">{t.pours}</td><td className="num">{fmtSigned(t.meanErrorMl)}</td><td className="num">{t.maeMl.toFixed(3)}</td></tr>)}</tbody>
            </table></div>
          )}
          {r.lowTubes.length > 0 && <p className="notice warn">Low tubes at report time: {r.lowTubes.join(", ")}</p>}
          {r.abortReasons.length > 0 && <p className="notice bad">Abort reasons: {r.abortReasons.join("; ")}</p>}
          <p className="cap">Tube volumes are a snapshot at report time. The recommendation is advice; it never changes settings or moves the arm.</p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Audit

function AuditTab() {
  const [q, setQ] = useState("");
  const all = useQuery(api.bench.auditLog, { limit: 150 });
  const found = useQuery(api.search.audit, q.trim() ? { text: q } : "skip");
  const log = q.trim() ? found : all;
  return (
    <div className="panel">
      <div className="between" style={{ marginBottom: 8 }}>
        <span className="label">Audit log · append-only</span>
        <input className="input" style={{ maxWidth: 280 }} placeholder="Search: swap, refused, well 3, a name…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search the audit log" />
        <span className="muted" style={{ fontSize: "0.8125rem" }}>Names are typed by each operator, not verified accounts.</span>
      </div>
      <div className="tbl"><table>
        <thead><tr><th>Time</th><th>Who</th><th>Action</th><th>Detail</th></tr></thead>
        <tbody>{log?.map((e) => (
          <tr key={e._id}><td className="mono" style={{ whiteSpace: "nowrap" }}>{clock(e.t)}</td><td>{e.actor}</td><td><span className={`chip ${e.action === "refused" || e.action === "stop" || e.action === "warn" ? "bad" : e.action === "approve" || e.action === "done" ? "ok" : ""}`}>{e.action}</span></td><td>{e.detail}</td></tr>
        ))}</tbody>
      </table></div>
      {log?.length === 0 && <p className="muted">{q ? "No entries match." : "Nothing logged yet."}</p>}
      <p className="cap">Every submit, check, approval, move, pour warning, bench change and report lands here and in Tiger Data.</p>
    </div>
  );
}

