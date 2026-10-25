import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../convex/_generated/api";
import type { Doc, Id } from "../convex/_generated/dataModel";
import { DYE, ErrorChart, Status, Via, ago, clock, fmtSigned, useName } from "./ui";
import Data from "./Data";
import Builder from "./Builder";
import LiveArm from "./robot/LiveArm";
import Cameras from "./Cameras";
import Chat from "./Chat";
import StreamField from "./StreamField";
import { APPROVE, CANCEL, STOP, readBack, useHoldToTalk, useSpeaker } from "./Voice";

const TABS = [
  ["run", "Run"], ["chat", "Assistant"], ["builder", "Protocol builder"], ["qc", "Run log & QC"], ["reports", "Reports"], ["audit", "Audit"], ["data", "Tiger Data"],
] as const;
type Tab = (typeof TABS)[number][0];

const EXAMPLES = ["Run the assay on sample A", "Only test reagent C on two wells", "Pour 1.5 ml of A into well 2", "Pour some blue"];

function msg(e: unknown) {
  const m = (e as Error)?.message ?? String(e);
  return m.replace(/^.*Uncaught Error: /s, "").replace(/\s+at .*$/s, "").replace(/\[Request ID.*?\]\s*/, "").trim();
}

function WaveMark() {
  return (
    <svg viewBox="0 0 26 14" aria-hidden="true">
      {[3.4, 7, 10.6].map((y, i) => (
        <path key={y} d={`M1 ${y}c6.2-2.8 12.2 2.8 18.4 0 2.3-1 4-1.6 5.8-2`} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" opacity={[1, 0.5, 0.24][i]} />
      ))}
    </svg>
  );
}

export default function Console() {
  const [tab, setTab] = useState<Tab>(() => {
    const q = new URLSearchParams(location.search).get("tab");
    if (q && TABS.some(([id]) => id === q)) return q as Tab;
    try {
      const saved = localStorage.getItem("laminar.tab");               // a tab that no longer exists -> Run
      return saved && TABS.some(([id]) => id === saved) ? (saved as Tab) : "run";
    } catch { return "run"; }
  });
  useEffect(() => { try { localStorage.setItem("laminar.tab", tab); } catch { /* ignore */ } }, [tab]);
  const [name, setName] = useName();
  const ensure = useMutation(api.bench.ensure);
  useEffect(() => { void ensure({}); }, [ensure]);
  const bench = useQuery(api.bench.state);
  const benchLive = bench != null;

  return (
    <main className="console">
      <StreamField />
      <div className="console-top">
        <div className="wrap">
          <div className="console-brand">
            <a className="mark" href="/" aria-label="Laminar — back to project page">
              <WaveMark /> Laminar
            </a>
            <span className="console-eyebrow">Console</span>
          </div>
          <div className="console-pills">
            <label className="name-pill" htmlFor="who">
              <span>You are</span>
              <input id="who" placeholder="Your name" value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <span className={`bench-pill${benchLive ? " live" : ""}`} title={benchLive ? "Bench stream is live" : "Bench stream not connected"}>
              <span className="dot" aria-hidden="true" />
              {benchLive ? "Bench · live" : "Bench · offline"}
            </span>
            <div className="segmented" role="navigation" aria-label="Switch between project page and console">
              <a href="/">Project</a>
              <a href="#console" aria-current="page">Console</a>
            </div>
          </div>
        </div>
      </div>
      <nav className="tabs" role="tablist" aria-label="Console sections">
        <div className="wrap">
          {TABS.map(([id, label]) => (
            <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>
          ))}
        </div>
      </nav>
      <div className="wrap">
        {tab === "run" && <RunTab name={name} />}
        {tab === "chat" && <Chat name={name} />}
        {tab === "builder" && <Builder name={name} onSent={() => setTab("run")} />}
        {tab === "qc" && <QcTab />}
        {tab === "reports" && <ReportsTab />}
        {tab === "audit" && <AuditTab />}
        {tab === "data" && <Data />}
      </div>
    </main>
  );
}

/* ──────────────────────────────────────────────────────────── Run tab ─── */

function MicIcon() {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M9 5a3 3 0 0 1 6 0v6a3 3 0 0 1-6 0V5M5 10v1a7 7 0 0 0 14 0v-1M12 18v3" /></svg>;
}

function RunTab({ name }: { name: string }) {
  const bench = useQuery(api.bench.state);
  const runs = useQuery(api.runs.list, { limit: 12 });
  const [selected, setSelected] = useState<Id<"runs"> | null>(null);
  const current = selected ?? runs?.[0]?._id ?? null;
  const detail = useQuery(api.runs.get, current ? { id: current } : "skip");
  const submit = useMutation(api.runs.submit);
  const approve = useMutation(api.runs.approve);
  const cancel = useMutation(api.runs.cancel);
  const [text, setText] = useState(EXAMPLES[0]);
  const [error, setError] = useState("");
  const [speakOn, setSpeakOn] = useState(() => { try { return localStorage.getItem("laminar.voice") !== "off"; } catch { return true; } });
  useEffect(() => { try { localStorage.setItem("laminar.voice", speakOn ? "on" : "off"); } catch { /* ignore */ } }, [speakOn]);
  const { say, speaking } = useSpeaker(speakOn);

  const act = async (f: () => Promise<unknown>) => { setError(""); try { await f(); } catch (e) { setError(msg(e)); } };

  async function handleSpoken(said: string) {
    const r = detail?.run;
    const status = r?.status;
    if (STOP.test(said)) { say("Stopping now.", true); if (r && (status === "running" || status === "approved" || status === "awaiting_approval")) await act(() => cancel({ id: r._id, by: `${name} (voice)`, reason: "Stopped by voice" })); return; }
    if (CANCEL.test(said)) { if (status === "awaiting_approval" && r) { await act(() => cancel({ id: r._id, by: `${name} (voice)`, reason: "Cancelled by voice before motion" })); say("Cancelled. Nothing moved."); } else say("There is no plan waiting to cancel."); return; }
    if (APPROVE.test(said)) { if (status === "awaiting_approval" && r) await act(() => approve({ id: r._id, by: `${name} (voice)` })); else say("There is no plan waiting for approval."); return; }
    if (/^\W*repeat\W*$/i.test(said)) { if (r?.status === "awaiting_approval") say(readBack(r)); else say("Nothing to repeat."); return; }
    if (/^\W*status\W*$/i.test(said)) {
      if (status === "running" && r?.moves) say(`Move ${r.step} of ${r.moves.length}. ${r.moves[(r.step ?? 1) - 1] ?? ""}`.replace(/[()_]/g, " "));
      else say(status ? `The run is ${status.replace("_", " ")}.` : "No run yet.");
      return;
    }
    setText(said);
    await act(async () => { const id = await submit({ request: said, requestedBy: name, via: "voice" }); setSelected(id); });
  }
  const mic = useHoldToTalk(name, handleSpoken);

  // read out plan status changes
  const seen = useRef<string>("");
  useEffect(() => {
    const r = detail?.run;
    if (!r) return;
    const key = `${r._id}:${r.status}:${r.summary ? 1 : 0}`;
    if (seen.current === key) return;
    const first = seen.current === "";
    seen.current = key;
    if (first) return;
    if (r.status === "awaiting_approval") say(readBack(r));
    else if (r.status === "rejected") say(r.question ?? `I can't run that. ${r.errors?.[0] ?? ""}`);
    else if (r.status === "running") say("Approved. Starting now.");
    else if (r.status === "done") say(`Done. ${r.result ?? ""}`);
    else if (r.status === "aborted") say("Stopped. Nothing else will move.", true);
  }, [detail?.run, say]);

  const run = detail?.run;
  const step = run?.step ?? 0;
  const moving = run?.status === "running";
  const move = moving ? run?.moves?.[Math.max(0, step - 1)] : undefined;
  const canApprove = run?.status === "awaiting_approval" && !!name.trim();

  // Progress numbers: delivered mL vs target mL (sum across pours in this run).
  const target = (run?.tasks ?? []).reduce((s, t) => s + t.pours.reduce((a, p) => a + p.ml, 0), 0);
  const delivered = (detail?.pours ?? []).reduce((a, p) => a + p.pouredMl, 0);
  const tol = 0.1 * target;

  return (
    <div className="run-grid">
      <div className="run-left">
        {/* Ask for a run */}
        <form className="card ask-form" onSubmit={(e) => { e.preventDefault(); void act(async () => { const id = await submit({ request: text, requestedBy: name }); setSelected(id); }); }}>
          <div className="ask-head">
            <span className="label">Ask for a run</span>
            <span className="chip">{name.trim() ? `as ${name}` : "name needed to approve"}</span>
          </div>
          <label className="sr-only" htmlFor="req">Request</label>
          <textarea id="req" className="input" value={text} onChange={(e) => setText(e.target.value)} rows={2} />
          <div className="chips" style={{ marginTop: 10 }}>
            {EXAMPLES.map((x) => <button type="button" key={x} className="chip filter" onClick={() => setText(x)}>{x}</button>)}
          </div>
          <div className="ask-row">
            <button type="submit" className="btn primary reagent" disabled={!text.trim()}>Plan this run</button>
            <button type="button" className={`mic${mic.recording ? " on" : ""}`} aria-label="Hold to talk" title="Hold to talk · request, approve, stop"
              disabled={mic.working}
              onPointerDown={(e) => { e.preventDefault(); void mic.start(); }} onPointerUp={mic.stop} onPointerLeave={() => mic.recording && mic.stop()}>
              <MicIcon />
            </button>
            <span className="ask-hint">
              {mic.recording ? "Listening… release to send" : mic.working ? "Transcribing with Cartesia Ink…" : speaking ? "Speaking (Cartesia Sonic)…" : "Hold to talk · request, approve, stop"}
            </span>
            <label className="speak-check">
              <input type="checkbox" checked={speakOn} onChange={(e) => setSpeakOn(e.target.checked)} />
              Speak replies
            </label>
          </div>
          {(error || mic.error) && <p className="err" role="alert" style={{ marginTop: 8 }}>{error || mic.error}</p>}
        </form>

        {/* Plan / Progress card */}
        {run && (moving || run.status === "running" || run.status === "approved" || detail?.pours.length ? (
          <ProgressCard run={run} target={target} delivered={delivered} tol={tol}
            onStop={() => act(() => cancel({ id: run._id, by: name || "operator", reason: "Stopped by operator" }))} />
        ) : (
          <PlanCard run={run} detail={detail} canApprove={canApprove}
            onApprove={() => act(() => approve({ id: run._id, by: name }))}
            onCancel={() => act(() => cancel({ id: run._id, by: name || "operator" }))} />
        ))}
      </div>

      <div className="run-right">
        <div className="arm-canvas">
          <LiveArm height={380} />
          <span className="arm-tag">{run?.status === "running" ? `Pouring · ${move ?? ""}` : "Arm at home · simulated until connected"}</span>
        </div>
        <Cameras />
        <div className="card bench-strip">
          <span className="label">Bench</span>
          <span className="bench-state">{moving ? `running · move ${step} of ${run?.moves?.length}` : "idle"}</span>
          <span className="muted">· {bench ? `${bench.tubes.length} tubes · ${bench.wells.length} wells` : "waiting on state"}</span>
        </div>
      </div>
    </div>
  );
}

function PlanCard({ run, detail, canApprove, onApprove, onCancel }: {
  run: Doc<"runs">;
  detail: { pours: Doc<"pours">[] } | null | undefined;
  canApprove: boolean;
  onApprove: () => void;
  onCancel: () => void;
}) {
  const verdict = run.status === "awaiting_approval" ? "ok" : run.status === "rejected" ? "bad" : "";
  return (
    <div className="card hero plan-card">
      <div className="plan-head">
        <span className="label">Plan · “{run.request}”</span>
        <span className="row" style={{ gap: 6 }}>
          <Via v={run.via} />
          <Status s={run.status} />
        </span>
      </div>
      {run.status === "checking" && <p className="notice">Planning and checking…</p>}
      {run.plannerNote && <p className="notice" style={{ marginBottom: 10 }}><span className="label">Read-back · {run.planner === "rules" ? "rule planner" : run.planner}</span><br />{run.plannerNote}</p>}
      {run.question && <p className="notice warn"><span className="label">Question back, nothing planned</span><br />{run.question}</p>}
      {!!run.errors?.length && (
        <div className="notice bad"><span className="label">Refused before motion</span>
          <ul>{run.errors.map((e) => <li key={e}>{e}</li>)}</ul>
        </div>
      )}
      {run.tasks && !!run.tasks.length && (
        <ol className="steps-list" aria-label="Planned pours">
          {run.tasks.flatMap((t, ti) => t.pours.map((p, pi) => (
            <li key={`${ti}-${pi}`}>
              <span className="step-n">{ti + 1}.{pi + 1}</span>
              <span className="step-action"><span className="dot" style={{ background: DYE[t.tube], marginRight: 8 }} />Tube {t.tube} → Well {p.well}{p.well === 4 && " · control"}</span>
              <span className="step-detail">{p.ml.toFixed(1)} mL</span>
            </li>
          )))}
        </ol>
      )}
      {run.editedBy && <p className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>Edited by {run.editedBy} · checked again</p>}
      {!!detail?.pours.length && (
        <div className="tbl" style={{ marginTop: 10 }}>
          <table>
            <thead><tr><th scope="col">Tube</th><th scope="col">Well</th><th scope="col" className="num">Target</th><th scope="col" className="num">Poured</th><th scope="col" className="num">Error</th></tr></thead>
            <tbody>
              {detail.pours.map((p) => (
                <tr key={p._id}>
                  <td><span className="dot" style={{ background: DYE[p.tube], marginRight: 6 }} />{p.tube}</td>
                  <td>{p.well}</td>
                  <td className="num">{p.targetMl.toFixed(2)}</td>
                  <td className="num">{p.pouredMl.toFixed(2)}</td>
                  <td className="num" style={{ color: Math.abs(p.errorMl) > 0.1 * p.targetMl ? "var(--flag)" : undefined }}>{fmtSigned(p.errorMl, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="row" style={{ marginTop: 14 }}>
        {run.status === "awaiting_approval" && (
          <>
            <button className="btn primary" disabled={!canApprove} onClick={onApprove}>Approve &amp; run · {run.moves?.length ?? 0} moves</button>
            <button className="btn tertiary">Edit plan</button>
            <button className="btn tertiary" style={{ marginLeft: "auto" }} onClick={onCancel}>Cancel</button>
            {!canApprove && <span className="approve-disabled">· enter your name above to approve</span>}
          </>
        )}
        {verdict === "" && run.status !== "checking" && (
          <span className="chip">status · {run.status}</span>
        )}
      </div>
    </div>
  );
}

function ProgressCard({ run, target, delivered, tol, onStop }: {
  run: Doc<"runs">; target: number; delivered: number; tol: number;
  onStop: () => void;
}) {
  const trackMax = Math.max(target * 1.2, 0.1);
  const bandLeftPct = target > 0 ? Math.max(0, ((target - tol) / trackMax) * 100) : 0;
  const bandWidthPct = target > 0 ? Math.min(100, ((2 * tol) / trackMax) * 100) : 0;
  const tickPct = target > 0 ? (target / trackMax) * 100 : 0;
  const fillPct = Math.min(100, (delivered / trackMax) * 100);
  return (
    <div className="card hero">
      <div className="plan-head">
        <span className="label">Run in progress · “{run.request}”</span>
        <span className="row" style={{ gap: 6 }}><Via v={run.via} /><Status s={run.status} /></span>
      </div>
      <div className="progress-mL">
        {delivered.toFixed(2)}<small>/ {target.toFixed(2)} mL</small>
      </div>
      <div className="tolerance-bar" role="img" aria-label={`${delivered.toFixed(2)} of ${target.toFixed(2)} millilitres delivered, within ±${tol.toFixed(2)}`}>
        <span className="band" style={{ left: `${bandLeftPct}%`, width: `${bandWidthPct}%` }} />
        <span className="tick" style={{ left: `calc(${tickPct}% - 1px)` }} />
        <span className="fill" style={{ width: `${fillPct}%` }} />
      </div>
      <div className="between t-label" style={{ color: "var(--ink-3)" }}>
        <span>Delivered · {delivered.toFixed(2)} mL</span>
        <span>Target · {target.toFixed(2)} mL · ±{tol.toFixed(2)}</span>
      </div>
      {run.moves && (
        <ol className="moves" style={{ marginTop: 12 }} aria-label="Move list">
          {run.moves.map((m, i) => (
            <li key={i} className={i < (run.step ?? 0) - 1 || run.status === "done" ? "done" : run.status === "running" && i === (run.step ?? 0) - 1 ? "now" : ""}>
              <span>{i + 1}</span><span>{m}</span>
            </li>
          ))}
        </ol>
      )}
      {run.result && <p className="notice ok" style={{ marginTop: 10 }}><span className="label">Measured readout</span><br />{run.result}</p>}
      {run.abortReason && <p className="notice bad" style={{ marginTop: 10 }}>Aborted: {run.abortReason}</p>}
      {run.summary && <p className="notice" style={{ marginTop: 10 }}><span className="label">Claude’s note</span><br />{run.summary}</p>}
      <div className="row" style={{ marginTop: 14 }}>
        <button className="btn tertiary" disabled>Pause</button>
        <button className="btn tertiary" disabled>Skip</button>
        <button className="btn destructive" onClick={onStop}>Stop</button>
      </div>
    </div>
  );
}

/* ──────────────────────────────────────────────────────────── QC tab ─── */

function QcTab() {
  const qc = useQuery(api.qc.recent, { limit: 120 });
  const [outOnly, setOutOnly] = useState(false);
  if (!qc) return <p className="muted" style={{ padding: 24 }}>Loading…</p>;
  const o = qc.overall;
  const pours = outOnly ? qc.pours.filter((p) => Math.abs(p.errorMl) > 0.1 * p.targetMl) : qc.pours;

  const exportCsv = () => {
    const rows = [["t_iso", "run_id", "tube", "well", "target_ml", "poured_ml", "error_ml", "error_pct", "result"].join(",")]
      .concat(qc.pours.map((p) => [
        new Date(p.t).toISOString(), p.runId ?? "", p.tube, p.well,
        p.targetMl.toFixed(3), p.pouredMl.toFixed(3), p.errorMl.toFixed(3),
        ((p.errorMl / p.targetMl) * 100).toFixed(1),
        Math.abs(p.errorMl) > 0.1 * p.targetMl ? "out" : "in"
      ].join(",")));
    const blob = new Blob([rows.join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `pours-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="stack" style={{ paddingTop: 20 }}>
      <div className="qc-strip">
        <div><span className="label">Pours</span><div className="fig">{o.n || "—"}</div><div className="sub">latest 120</div></div>
        <div><span className="label">Within ±10%</span><div className={`fig${o.inBandPct != null ? " jade" : ""}`}>{o.inBandPct != null ? `${o.inBandPct}%` : "—"}</div><div className="sub">goal: every pour</div></div>
        <div><span className="label">Mean error</span><div className="fig">{o.meanErrorMl != null ? fmtSigned(o.meanErrorMl) : "—"}</div><div className="sub">mL, signed</div></div>
        <div><span className="label">Mean abs error</span><div className="fig">{o.maeMl != null ? o.maeMl.toFixed(3) : "—"}</div><div className="sub">mL</div></div>
        <div><span className="label">Spread</span><div className="fig">{o.sdMl != null ? o.sdMl.toFixed(3) : "—"}</div><div className="sub">mL, std dev</div></div>
        <div><span className="label">Runs finished</span><div className="fig">{qc.runSuccessPct != null ? `${qc.runSuccessPct}%` : "—"}</div><div className="sub">done vs aborted</div></div>
        <div><span className="label">Plans refused</span><div className={`fig${(qc.refusals ?? 0) > 0 ? " flag" : ""}`}>{qc.refusals ?? "—"}</div><div className="sub">by the checker</div></div>
      </div>

      <section className="card">
        <div className="between" style={{ marginBottom: 10 }}>
          <span className="label">Pour error · % of target · last {qc.pours.length || 120}</span>
          <div className="chips">
            {(["A", "B", "C", "D"] as const).map((t) => <span key={t} className="chip"><span className="dot" style={{ background: DYE[t] }} /> {t}</span>)}
            <span className="chip ok">±10% band</span>
          </div>
        </div>
        {qc.pours.length ? <ErrorChart pours={qc.pours} /> : <p className="muted" style={{ padding: "48px 0", textAlign: "center" }}>No pours yet. Run the assay and each pour appears here as it lands.</p>}
      </section>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 20 }}>
        <section className="card">
          <p className="label" style={{ marginBottom: 10 }}>Per tube</p>
          <div className="tbl">
            <table>
              <thead><tr><th scope="col">Tube</th><th scope="col" className="num">Pours</th><th scope="col" className="num">Mean</th><th scope="col" className="num">MAE</th><th scope="col" className="num">In band</th></tr></thead>
              <tbody>
                {qc.perTube.map((t) => (
                  <tr key={t.tube}>
                    <td><span className="dot" style={{ background: DYE[t.tube], marginRight: 6 }} />{t.tube}</td>
                    <td className="num">{t.n}</td>
                    <td className="num">{fmtSigned(t.meanErrorMl)}</td>
                    <td className="num">{t.maeMl != null ? t.maeMl.toFixed(3) : "—"}</td>
                    <td className="num" style={{ color: t.inBandPct != null && t.inBandPct < 100 ? "var(--flag)" : "var(--jade)" }}>{t.inBandPct != null ? `${t.inBandPct}%` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        <section className="card">
          <p className="label" style={{ marginBottom: 10 }}>Recent aborts</p>
          {qc.aborts.length === 0 ? <p className="muted">None.</p> : (
            <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: 10 }}>
              {qc.aborts.map((a, i) => (
                <li key={i} style={{ borderBottom: "1px solid var(--rule-2)", paddingBottom: 10 }}>
                  <b>{a.reason}</b>
                  <div className="mono muted" style={{ fontSize: 11.5, marginTop: 3 }}>{clock(a.t)} · {ago(a.t)}</div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <section className="card">
        <div className="between" style={{ marginBottom: 10 }}>
          <span className="label">Every pour</span>
          <div className="chips">
            <button type="button" className="chip filter" aria-pressed={outOnly} onClick={() => setOutOnly((v) => !v)}>Out of band only</button>
            <button type="button" className="chip filter" onClick={exportCsv}>Export CSV</button>
          </div>
        </div>
        <div className="tbl">
          <table>
            <thead><tr><th scope="col">Time</th><th scope="col">Run</th><th scope="col">Tube</th><th scope="col">Well</th><th scope="col" className="num">Target</th><th scope="col" className="num">Poured</th><th scope="col" className="num">Error %</th><th scope="col">Result</th></tr></thead>
            <tbody>
              {pours.map((p, i) => {
                const errPct = (p.errorMl / p.targetMl) * 100;
                const out = Math.abs(errPct) > 10;
                return (
                  <tr key={i} style={{ background: out ? "rgba(155, 44, 44, .05)" : undefined }}>
                    <td className="mono" style={{ fontSize: 12.5 }}>{clock(p.t)}</td>
                    <td className="mono" style={{ fontSize: 12.5 }}>{p.runId ? p.runId.slice(-6) : "—"}</td>
                    <td><span className="dot" style={{ background: DYE[p.tube], marginRight: 6 }} />{p.tube}</td>
                    <td>{p.well}</td>
                    <td className="num">{p.targetMl.toFixed(2)}</td>
                    <td className="num">{p.pouredMl.toFixed(2)}</td>
                    <td className="num" style={{ color: out ? "var(--flag)" : undefined }}>{errPct > 0 ? "+" : ""}{errPct.toFixed(1)}%</td>
                    <td>{out ? <span className="chip bad">Out of band</span> : <span className="chip ok">In band</span>}</td>
                  </tr>
                );
              })}
              {pours.length === 0 && <tr><td colSpan={8} className="muted" style={{ padding: 20 }}>{outOnly ? "No out-of-band pours in this window." : "No pours yet."}</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

/* ──────────────────────────────────────────────────────────── Reports tab ─── */

function ReportsTab() {
  const reports = useQuery(api.reports.list);
  const gen = useMutation(api.reports.generateNow);
  const [sel, setSel] = useState<string | null>(null);
  const r = reports?.find((x) => x._id === sel) ?? reports?.[0];
  return (
    <div className="reports-grid">
      <section className="card reports-main">
        <div className="between" style={{ marginBottom: 14 }}>
          <div>
            <span className="label">Run reports</span>
            <p style={{ fontSize: 13.5, marginTop: 4, color: "var(--ink-3)" }}>Generated at 08:00 Los Angeles time from the previous 24 hours. Frozen once written.</p>
          </div>
          <button className="btn primary reagent" onClick={() => gen({})}>Generate now</button>
        </div>
        {reports?.length === 0 && <p className="muted">No reports yet. The next scheduled one runs at 08:00 Los Angeles.</p>}
        {!!reports?.length && (
          <div className="tbl">
            <table>
              <thead><tr><th scope="col">Date</th><th scope="col">Trigger</th><th scope="col" className="num">Pours</th><th scope="col" className="num">In band</th><th scope="col" className="num">MAE</th><th scope="col">Status</th><th scope="col">Open</th></tr></thead>
              <tbody>
                {reports.map((x) => (
                  <tr key={x._id} aria-current={x._id === r?._id} style={{ cursor: "pointer" }} onClick={() => setSel(x._id)}>
                    <td className="mono" style={{ fontSize: 12.5 }}>{x.date}</td>
                    <td>{x.trigger === "schedule" ? "scheduled" : "manual"}</td>
                    <td className="num">{x.pours}</td>
                    <td className="num">{x.inBandPct != null ? `${x.inBandPct}%` : "—"}</td>
                    <td className="num">{x.meanErrorMl != null ? fmtSigned(x.meanErrorMl) : "—"}</td>
                    <td>{x.aborted > 0 ? <span className="chip bad">{x.aborted} aborted</span> : <span className="chip ok">done</span>}</td>
                    <td className="mono" style={{ fontSize: 12.5 }}><button type="button" className="link-btn" onClick={(e) => { e.stopPropagation(); setSel(x._id); }}>View</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {r && (
          <div className="stack" style={{ gap: 14, marginTop: 20, borderTop: "1px solid var(--rule)", paddingTop: 16 }}>
            <div>
              <span className="label">{r.date} · {new Date(r.windowFrom).toLocaleString()} → {new Date(r.windowTo).toLocaleTimeString()}</span>
              <p className="notice" style={{ marginTop: 10 }}><span className="label">Recommendation</span><br />{r.recommendation}</p>
            </div>
            <div className="tiles">
              <div><span className="label">Runs</span><b>{r.runs}</b><small>{r.done} done · {r.aborted} aborted</small></div>
              <div><span className="label">Pours</span><b>{r.pours}</b></div>
              <div><span className="label">Within ±10%</span><b>{r.inBandPct != null ? `${r.inBandPct}%` : "—"}</b></div>
              <div><span className="label">Mean error</span><b>{r.meanErrorMl != null ? fmtSigned(r.meanErrorMl) : "—"}</b><small>mL</small></div>
            </div>
            {r.perTube.length > 0 && (
              <div className="tbl">
                <table>
                  <thead><tr><th scope="col">Tube</th><th scope="col" className="num">Pours</th><th scope="col" className="num">Mean error</th><th scope="col" className="num">MAE</th></tr></thead>
                  <tbody>
                    {r.perTube.map((t) => (
                      <tr key={t.tube}>
                        <td><span className="dot" style={{ background: DYE[t.tube], marginRight: 6 }} />{t.tube}</td>
                        <td className="num">{t.pours}</td>
                        <td className="num">{fmtSigned(t.meanErrorMl)}</td>
                        <td className="num">{t.maeMl.toFixed(3)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </section>
      <aside className="card reports-aside">
        <span className="label" style={{ marginBottom: 10, display: "block" }}>What a report contains</span>
        <ol className="numbered">
          <li>The protocol as approved, with the operator who approved it and the timestamp.</li>
          <li>Every pour: tube, well, target, poured, error, result.</li>
          <li>The QC summary and the pour-error chart.</li>
          <li>Every checker verdict and every override.</li>
          <li>The audit excerpt for the window.</li>
        </ol>
        <p className="cap" style={{ marginTop: 16 }}>PDF for the binder, CSV for the LIMS.</p>
      </aside>
    </div>
  );
}

/* ──────────────────────────────────────────────────────────── Audit tab ─── */

const ACTOR_FILTERS = [
  { id: "all", label: "All" },
  { id: "op", label: "Operator" },
  { id: "claude", label: "Claude" },
  { id: "checker", label: "Checker" },
  { id: "arm", label: "Arm" },
] as const;
type ActorKey = (typeof ACTOR_FILTERS)[number]["id"];

function actorClass(actor: string): string {
  const a = actor.toLowerCase();
  if (a.includes("claude")) return "actor-claude";
  if (a.includes("checker")) return "actor-checker";
  if (a.includes("arm")) return "actor-arm";
  return "actor-op";
}

function AuditTab() {
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<ActorKey>("all");
  const all = useQuery(api.bench.auditLog, { limit: 200 });
  const found = useQuery(api.search.audit, q.trim() ? { text: q } : "skip");
  const log = q.trim() ? found : all;
  const filtered = (log ?? []).filter((e) => {
    if (filter === "all") return true;
    const a = e.actor.toLowerCase();
    if (filter === "claude") return a.includes("claude");
    if (filter === "checker") return a.includes("checker");
    if (filter === "arm") return a.includes("arm");
    return !a.includes("claude") && !a.includes("checker") && !a.includes("arm");
  });

  const exportAudit = () => {
    const rows = [["t_iso", "actor", "action", "detail"].join(",")].concat(
      (log ?? []).map((e) => [new Date(e.t).toISOString(), JSON.stringify(e.actor), e.action, JSON.stringify(e.detail)].join(","))
    );
    const blob = new Blob([rows.join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `audit-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="reports-grid">
      <section className="card reports-main">
        <div className="audit-head">
          <span className="label">Who did what · append-only</span>
          <input className="input" style={{ maxWidth: 280, flex: "1 1 180px" }} placeholder="Search: swap, refused, well 3, a name…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search the audit log" />
        </div>
        <div className="chips" style={{ marginBottom: 14 }}>
          {ACTOR_FILTERS.map((f) => (
            <button key={f.id} type="button" className="chip filter" aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>{f.label}</button>
          ))}
        </div>
        <div className="tbl">
          <table className="audit-table">
            <thead><tr><th scope="col">Time</th><th scope="col">Actor</th><th scope="col">Event</th><th scope="col">Detail</th></tr></thead>
            <tbody>
              {filtered.map((e) => (
                <tr key={e._id} className={e.action === "approve" ? "approval" : ""}>
                  <td className="time">{clock(e.t)}</td>
                  <td className={actorClass(e.actor)}>{e.actor}</td>
                  <td><span className={`chip ${e.action === "refused" || e.action === "stop" || e.action === "warn" ? "bad" : e.action === "approve" || e.action === "done" ? "ok" : ""}`}>{e.action}</span></td>
                  <td>{e.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {filtered.length === 0 && <p className="muted" style={{ marginTop: 10 }}>{q ? "No entries match." : "Nothing logged yet."}</p>}
      </section>
      <aside className="card reports-aside">
        <span className="label" style={{ marginBottom: 10, display: "block" }}>What gets recorded</span>
        <ul className="square-bullet">
          <li>Every request typed or spoken, with the operator's name.</li>
          <li>Every plan and every checker verdict.</li>
          <li>Every approval, with a hash of the plan that was approved.</li>
          <li>Every pour, stop, skip and override.</li>
        </ul>
        <p className="cap" style={{ marginTop: 16 }}>Append only. Nothing here can be edited from the console.</p>
        <button className="btn tertiary" style={{ marginTop: 14 }} onClick={exportAudit}>Export audit</button>
      </aside>
    </div>
  );
}
