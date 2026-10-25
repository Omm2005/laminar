import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../convex/_generated/api";
import type { Doc, Id } from "../convex/_generated/dataModel";
import { DYE, STANDS, Status, clock, fmtSigned } from "./ui";
import { APPROVE, CANCEL, STOP, readBack, useHoldToTalk, useSpeaker } from "./Voice";

/** Chat with Claude. Plans come back as plan cards; nothing moves until a named person approves one. */

const THREAD_KEY = "laminar.chat.thread";
const QUICK = ["Status", "What's left in vial A?", "Pause after this channel"];
const EXAMPLES = ["Run the standard assay", "Pour 1.5 mL of A into well 2", "Only test reagent C on two wells", "How did the last run go?"];

const newThread = () => `t_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
function useThread(): [string, () => void] {
  const [id, setId] = useState(() => { try { return localStorage.getItem(THREAD_KEY) || newThread(); } catch { return newThread(); } });
  useEffect(() => { try { localStorage.setItem(THREAD_KEY, id); } catch { /* private mode */ } }, [id]);
  return [id, () => setId(newThread())];
}

function msg(e: unknown) {
  const m = (e as Error)?.message ?? String(e);
  return m.replace(/^.*Uncaught Error: /s, "").replace(/\s+at .*$/s, "").replace(/\[Request ID.*?\]\s*/, "").trim();
}

function MicIcon() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M9 5a3 3 0 0 1 6 0v6a3 3 0 0 1-6 0V5M5 10v1a7 7 0 0 0 14 0v-1M12 18v3" /></svg>;
}

export default function Chat({ name }: { name: string }) {
  const [threadId, reset] = useThread();
  const messages = useQuery(api.messages.list, { threadId });
  const active = useQuery(api.messages.active, { threadId });
  const bench = useQuery(api.bench.state);
  const send = useMutation(api.messages.send);
  const decide = useMutation(api.messages.decide);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [speakOn, setSpeakOn] = useState(() => { try { return localStorage.getItem("laminar.voice") !== "off"; } catch { return true; } });
  useEffect(() => { try { localStorage.setItem("laminar.voice", speakOn ? "on" : "off"); } catch { /* ignore */ } }, [speakOn]);
  const { say, speaking } = useSpeaker(speakOn);

  const pending = active?.status === "awaiting_approval" ? active : null;
  const moving = active && (active.status === "running" || active.status === "approved") ? active : null;

  async function submit(raw: string, via: "chat" | "voice") {
    const text = raw.trim();
    if (!text) return;
    setError("");
    if (!name.trim()) { setError("Enter your name at the top first, so the log records who asked."); return; }
    setDraft("");
    const target = moving ?? pending;
    try {
      if (STOP.test(text) && target) { if (via === "voice") say("Stopping now.", true); await decide({ threadId, runId: target._id, decision: "stop", by: name, via, said: text }); return; }
      if (pending && APPROVE.test(text)) { await decide({ threadId, runId: pending._id, decision: "approve", by: name, via, said: text }); return; }
      if (pending && CANCEL.test(text)) { await decide({ threadId, runId: pending._id, decision: "cancel", by: name, via, said: text }); return; }
      if (pending && /^\W*repeat\W*$/i.test(text)) { say(readBack(pending)); return; }
      await send({ threadId, text, by: name, via });
    } catch (e) { setError(msg(e)); setDraft(text); }
  }
  const mic = useHoldToTalk(name, (text) => submit(text, "voice"));

  const lastSeen = useRef<string | null>(null);
  useEffect(() => {
    const last = messages?.at(-1);
    if (!last) return;
    if (lastSeen.current === null) { lastSeen.current = last._id; return; }
    if (last._id === lastSeen.current) return;
    lastSeen.current = last._id;
    if (last.role !== "assistant") return;
    if (last.runId && pending?._id === last.runId) say(`${last.text} ${readBack(pending)}`);
    else if (!last.runId) say(last.text);
  }, [messages, pending, say]);

  const log = useRef<HTMLDivElement>(null);
  const thinking = messages?.at(-1)?.role === "user";
  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" }); }, [messages?.length, thinking]);
  const lastCard = [...(messages ?? [])].reverse().find((m) => m.runId)?.runId;

  const step = moving?.step ?? 0;

  return (
    <div className="chat-grid">
      <section className="card chat-col chat">
        <div className="chat-head">
          <span className="label">Ask the bench</span>
          <span className="chat-head-note">Reads the same state the Run tab does</span>
        </div>
        <div className="chat-log" ref={log} role="log" aria-live="polite">
          {messages?.length === 0 && (
            <div className="chat-empty">
              <h3>Tell the robot what to pour.</h3>
              <p className="muted">Type or hold the mic. Every plan is checked against the bench, and nothing moves until you approve it.</p>
              <div className="chips">{EXAMPLES.map((x) => <button key={x} type="button" className="chip filter" onClick={() => void submit(x, "chat")}>{x}</button>)}</div>
            </div>
          )}
          {messages?.map((m) => m.runId
            ? <PlanCard key={m._id} m={m} runId={m.runId} name={name} threadId={threadId} say={say} latest={m.runId === lastCard} />
            : <div key={m._id} className={m.role === "user" ? "msg-user" : "msg-bot"}>
                {m.text}
                {m.role === "assistant" && (
                  <span className="source">From the live run state · {clock(m.t)}</span>
                )}
              </div>)}
          {thinking && <div className="msg-bot muted">Thinking…</div>}
        </div>
        <div className="chips" style={{ padding: "8px 0 0" }}>
          {QUICK.map((q) => <button key={q} type="button" className="chip filter" onClick={() => void submit(q, "chat")}>{q}</button>)}
        </div>
        <form className="chat-input" onSubmit={(e) => { e.preventDefault(); void submit(draft, "chat"); }}>
          <button type="button" className={`mic small${mic.recording ? " on" : ""}`} aria-label="Hold to talk" title="Hold to talk" disabled={mic.working}
            onPointerDown={(e) => { e.preventDefault(); void mic.start(); }} onPointerUp={mic.stop} onPointerLeave={() => mic.recording && mic.stop()}>
            <MicIcon />
          </button>
          <input className="input" value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="Message the assistant"
            placeholder={mic.recording ? "Listening… release to send" : mic.working ? "Transcribing…" : pending ? "Ask, or type approve / cancel…" : moving ? "Ask, or type stop…" : "Ask for a run, or a question about the bench…"} />
          <button className="btn primary" disabled={!draft.trim()}>Send</button>
        </form>
        <div className="between" style={{ marginTop: 8 }}>
          <span className="err">{error || mic.error}</span>
          <label className="speak-check">
            <input type="checkbox" checked={speakOn} onChange={(e) => setSpeakOn(e.target.checked)} />
            {speaking ? "Speaking…" : "Speak replies"}
          </label>
          {!!messages?.length && <button type="button" className="link-btn" onClick={() => { reset(); setError(""); }} style={{ marginLeft: 10 }}>New conversation</button>}
        </div>
      </section>

      <aside className="chat-aside">
        <div className="card">
          <span className="label" style={{ marginBottom: 10, display: "block" }}>What it can do</span>
          <ul className="square-bullet">
            <li>Answer from the live state, the audit log and Tiger traces.</li>
            <li>Queue a pause, stop or redo on the next pour.</li>
            <li>Cannot start a pour without an approval on the Run tab.</li>
          </ul>
        </div>
        <div className="card">
          <span className="label" style={{ marginBottom: 10, display: "block" }}>Right now</span>
          {moving ? (
            <div className="stack" style={{ gap: 6 }}>
              <p>Running: <b>“{moving.request}”</b></p>
              <p className="mono" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>Move {step} of {moving.moves?.length ?? 0}</p>
            </div>
          ) : pending ? (
            <div className="stack" style={{ gap: 6 }}>
              <p>Plan waiting on approval: <b>“{pending.request}”</b></p>
              <p className="mono" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>Go to the Run tab to approve.</p>
            </div>
          ) : bench ? (
            <div className="stack" style={{ gap: 8 }}>
              <p className="mono" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>Idle · {bench.tubes.length} tubes · {bench.wells.length} wells</p>
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 4 }}>
                {bench.tubes.map((t) => (
                  <li key={t._id} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13.5 }}>
                    <span className="dot" style={{ background: DYE[t.letter] }} />
                    <span>{t.letter}</span>
                    <span className="muted">· {STANDS[t.letter]}</span>
                    <span className="num mono" style={{ marginLeft: "auto" }}>{t.volumeMl.toFixed(2)} mL</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : <p className="muted">Waiting for bench state.</p>}
        </div>
      </aside>
    </div>
  );
}

/** A plan the assistant proposed, live from its run. */
function PlanCard({ m, runId, name, threadId, say, latest }: {
  m: Doc<"messages">; runId: Id<"runs">; name: string; threadId: string; say: (t: string, urgent?: boolean) => void; latest: boolean;
}) {
  const detail = useQuery(api.runs.get, { id: runId });
  const decide = useMutation(api.messages.decide);
  const [showMoves, setShowMoves] = useState(false);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const run = detail?.run;

  const seen = useRef<string | null>(null);
  useEffect(() => {
    if (!run || !latest) return;
    const key = `${run.status}:${run.summary ? 1 : 0}`;
    if (seen.current === null || seen.current === key) { seen.current = key; return; }
    seen.current = key;
    if (run.status === "running") say("Starting now.");
    else if (run.status === "done") say(`Done. ${run.summary ?? run.result ?? ""}`);
    else if (run.status === "aborted") say("Stopped. Nothing else will move.", true);
  }, [run, latest, say]);

  if (!run) return <div className="plan-card muted">Loading plan…</div>;
  const act = (decision: "approve" | "cancel" | "stop") =>
    decide({ threadId, runId, decision, by: name, via: "chat" }).catch((e) => setError(msg(e)));
  const step = run.step ?? 0;
  const moving = run.status === "running";

  return (
    <div className={`plan-card${run.status === "awaiting_approval" ? " waiting" : ""}`}>
      <div className="between">
        <span className="label">Plan · {run.planner === "rules" ? "rule planner" : run.planner}</span>
        <Status s={run.status} />
      </div>
      <p style={{ margin: "6px 0 10px" }}>{m.text}</p>
      {editing && run.status === "awaiting_approval" ? (
        <PlanEditor run={run} name={name} onDone={() => setEditing(false)} />
      ) : (
        <div className="plan-pours">
          {(run.tasks ?? []).flatMap((t) => t.pours.map((p, i) => (
            <div key={`${t.tube}${i}`} className="plan-pour">
              <b className="num">{p.ml.toFixed(1)} mL</b>
              <span><span className="dot" style={{ background: DYE[t.tube] }} /> Tube {t.tube} <span className="muted">· {STANDS[t.tube]}</span></span>
              <span className="muted">→</span>
              <span>Well {p.well}{p.well === 4 && <span className="muted"> · control</span>}</span>
            </div>
          )))}
        </div>
      )}
      {run.editedBy && <p className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>Edited by {run.editedBy} · checked again</p>}
      {!!run.errors?.length && <div className="notice bad" style={{ marginTop: 10 }}><span className="label">Not run</span><ul>{run.errors.map((e) => <li key={e}>{e}</li>)}</ul></div>}
      {moving && <p className="notice" style={{ marginTop: 10 }}>Move {step} of {run.moves?.length}: <span className="mono">{run.moves?.[Math.max(0, step - 1)]}</span></p>}
      {run.abortReason && <p className="notice bad" style={{ marginTop: 10 }}>Stopped: {run.abortReason}</p>}
      {run.result && <p className="notice ok" style={{ marginTop: 10 }}><span className="label">Measured readout</span><br />{run.result}</p>}
      {run.summary && <p className="notice" style={{ marginTop: 10 }}><span className="label">Claude's note</span><br />{run.summary}</p>}
      {!!detail?.pours.length && (
        <div className="tbl" style={{ marginTop: 10 }}><table>
          <thead><tr><th scope="col">Tube</th><th scope="col">Well</th><th scope="col" className="num">Target</th><th scope="col" className="num">Poured</th><th scope="col" className="num">Error</th></tr></thead>
          <tbody>{detail.pours.map((p) => (
            <tr key={p._id}><td><span className="dot" style={{ background: DYE[p.tube] }} /> {p.tube}</td><td>{p.well}</td>
              <td className="num">{p.targetMl.toFixed(2)}</td><td className="num">{p.pouredMl.toFixed(2)}</td>
              <td className="num" style={{ color: Math.abs(p.errorMl) > 0.1 * p.targetMl ? "var(--flag)" : undefined }}>{fmtSigned(p.errorMl, 2)}</td></tr>
          ))}</tbody>
        </table></div>
      )}
      {run.moves && (showMoves
        ? <ol className="moves" style={{ marginTop: 10 }} aria-label="Move list">{run.moves.map((x, i) => (
            <li key={i} className={i < step - 1 || run.status === "done" ? "done" : moving && i === step - 1 ? "now" : ""}><span>{i + 1}</span><span>{x}</span></li>
          ))}</ol>
        : <button className="link-btn" style={{ marginTop: 8 }} onClick={() => setShowMoves(true)}>Show the {run.moves.length} robot moves</button>)}
      {error && <p className="err">{error}</p>}
      <div className="row" style={{ marginTop: 10 }}>
        {run.status === "awaiting_approval" && !editing && <>
          <button className="btn primary" disabled={!name.trim()} onClick={() => void act("approve")}>Approve {run.moves?.length} moves</button>
          <button className="btn tertiary" onClick={() => setEditing(true)}>Edit plan</button>
          <button className="btn tertiary" onClick={() => void act("cancel")}>Cancel</button>
          {!name.trim() && <span className="approve-disabled">· enter your name to approve</span>}
        </>}
        {(moving || run.status === "approved") && <button className="btn destructive" onClick={() => void act("stop")}>Stop</button>}
        {run.approvedBy && <span className="muted" style={{ fontSize: 12.5 }}>Approved by {run.approvedBy}</span>}
      </div>
    </div>
  );
}

type Row = { tube: "A" | "B" | "C" | "D"; well: number; ml: number };

function PlanEditor({ run, name, onDone }: { run: Doc<"runs">; name: string; onDone: () => void }) {
  const edit = useMutation(api.runs.editPlan);
  const [rows, setRows] = useState<Row[]>(() => (run.tasks ?? []).flatMap((t) => t.pours.map((p) => ({ tube: t.tube, well: p.well, ml: p.ml }))));
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const set = (i: number, patch: Partial<Row>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  async function save() {
    if (!name.trim()) { setErrors(["Enter your name at the top first, so the log records who changed the plan."]); return; }
    const tasks: { tube: Row["tube"]; pours: { well: number; ml: number }[] }[] = [];
    for (const r of rows) {
      const t = tasks.find((x) => x.tube === r.tube) ?? tasks[tasks.push({ tube: r.tube, pours: [] }) - 1];
      t.pours.push({ well: r.well, ml: Math.round(r.ml * 100) / 100 });
    }
    setBusy(true); setErrors([]);
    try {
      const r = await edit({ id: run._id, tasks, by: name });
      if (r.ok) onDone(); else setErrors(r.errors);
    } catch (e) { setErrors([msg(e)]); }
    setBusy(false);
  }

  return (
    <div className="plan-editor">
      {rows.map((r, i) => (
        <div key={i} className="plan-edit-row">
          <select className="input" value={r.tube} aria-label="Tube" onChange={(e) => set(i, { tube: e.target.value as Row["tube"] })}>
            {(["A", "B", "C", "D"] as const).map((t) => <option key={t} value={t}>Tube {t} · {STANDS[t]}</option>)}
          </select>
          <span className="muted">→</span>
          <select className="input" value={r.well} aria-label="Well" onChange={(e) => set(i, { well: Number(e.target.value) })}>
            {[1, 2, 3, 4].map((w) => <option key={w} value={w}>Well {w}{w === 4 ? " · control" : ""}</option>)}
          </select>
          <input className="input num" type="number" step={0.1} min={0.2} max={2} value={r.ml} aria-label="Millilitres"
            onChange={(e) => set(i, { ml: Number(e.target.value) })} />
          <span className="muted">mL</span>
          <button type="button" className="btn small" aria-label="Remove this pour" onClick={() => setRows(rows.filter((_, j) => j !== i))}>Remove</button>
        </div>
      ))}
      <button type="button" className="link-btn" onClick={() => setRows([...rows, rows.at(-1) ? { ...rows.at(-1)! } : { tube: "A", well: 1, ml: 1 }])}>+ Add a pour</button>
      {!!errors.length && <div className="notice bad" style={{ marginTop: 8 }}><span className="label">The checker refused this change</span><ul>{errors.map((e) => <li key={e}>{e}</li>)}</ul></div>}
      <div className="row" style={{ marginTop: 10 }}>
        <button type="button" className="btn primary" disabled={busy || !rows.length} onClick={() => void save()}>{busy ? "Checking…" : "Save and re-check"}</button>
        <button type="button" className="btn tertiary" disabled={busy} onClick={onDone}>Discard changes</button>
        <span className="muted" style={{ fontSize: 12.5 }}>Pours 0.2–2.0 mL · well 4 only gets sample A</span>
      </div>
    </div>
  );
}
