import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";
import type { Doc, Id } from "../../convex/_generated/dataModel";
import { approvalWord, canListen, canSpeak, speak, useListen } from "../voice";
import { STATUS_WORDS, errText, notReadyReason, useStation } from "../station";
import { Close } from "../components/Toasts";

/** The Lab: a chat with the assistant, typed or spoken. Plans come back as permission cards; nothing moves until a
 *  person approves one. Closing a card without deciding keeps it as a pending pill above the input. */

type PlanPour = { vial: number; container: number; ml: number; vialLetter: string; vialName: string;
                  containerName: string; containerMlNow: number; capacityMl: number };
type PlanStep = { name: string; label: string; placeholder?: boolean };
type PlanArgs = { jobId: Id<"jobs">; pours: PlanPour[]; steps: PlanStep[] };
type Msg = Doc<"messages">;

const APPROVER_KEY = "laminar.approver";
const SPEAK_KEY = "laminar.speak";
const CLOSED_KEY = "laminar.closedPermissions";

function useStored(key: string, initial: string): [string, (v: string) => void] {
  const [v, setV] = useState(() => { try { return localStorage.getItem(key) ?? initial; } catch { return initial; } });
  return [v, (x: string) => { setV(x); try { localStorage.setItem(key, x); } catch { /* private mode */ } }];
}

/** Permission cards closed without a decision (kept as the pending pill). */
function useClosed(): [Set<string>, (id: string, closed: boolean) => void] {
  const [raw, setRaw] = useStored(CLOSED_KEY, "[]");
  const set = useMemo(() => { try { return new Set<string>(JSON.parse(raw)); } catch { return new Set<string>(); } }, [raw]);
  return [set, (id, closed) => {
    const next = new Set(set);
    if (closed) next.add(id); else next.delete(id);
    setRaw(JSON.stringify([...next].slice(-50)));
  }];
}

export default function Lab() {
  const messages = useQuery(api.messages.list, {}) as Msg[] | undefined;
  const pending = useQuery(api.jobs.pending, {});
  const inbox = useQuery(api.remote.inbox, {});
  const send = useMutation(api.messages.send);
  const approve = useMutation(api.jobs.approve);
  const reject = useMutation(api.jobs.reject);
  const [approver, setApprover] = useStored(APPROVER_KEY, "");
  const [speakOn, setSpeakOn] = useStored(SPEAK_KEY, "off");
  const [closed, setClosed] = useClosed();
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [thinking, setThinking] = useState(false);

  async function submit(text: string, via: "chat" | "voice") {
    const t = text.trim();
    if (!t) return;
    setDraft("");
    const word = approvalWord(t);
    if (word && pending) {                         // a person said / typed approve or reject
      if (!approver.trim()) {
        const msg = "Add your name next to the input first, so the approval is signed.";
        setNote(msg); if (via === "voice") speak(msg);
        return;
      }
      setNote(null);
      try {
        if (word === "approve") await approve({ jobId: pending._id, approvedBy: approver.trim(), via });
        else await reject({ jobId: pending._id, approvedBy: approver.trim(), via, reason: `rejected by ${via}` });
      } catch (e) { const msg = errText(e); setNote(msg); if (via === "voice") speak(msg); }
      return;
    }
    setNote(null);
    setThinking(true);
    await send({ text: t });
  }

  const mic = useListen((text) => submit(text, "voice"));

  // the assistant answered: stop the "thinking" line; read it aloud if asked
  const lastId = useRef<string | null>(null);
  useEffect(() => {
    if (!messages?.length) return;
    const last = messages[messages.length - 1];
    if (last.role !== "user") setThinking(false);
    if (lastId.current === null) { lastId.current = last._id; return; }
    if (last._id === lastId.current) return;
    lastId.current = last._id;
    if (speakOn === "on" && last.role === "assistant") {
      speak(last.tool?.name === "plan" ? planSentence(last.tool.args as PlanArgs) : last.text);
    }
  }, [messages, speakOn]);

  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "end", behavior: "smooth" }); }, [messages?.length, thinking]);

  const pendingClosed = pending && closed.has(pending._id);
  function reopen() {
    if (!pending) return;
    setClosed(pending._id, false);
    setTimeout(() => document.getElementById(`perm-${pending._id}`)?.scrollIntoView({ block: "center", behavior: "smooth" }), 50);
  }

  return (
    <div className="lab2">
      <div className="lab2-thread" role="log" aria-live="polite">
        {!!inbox?.length && (
          <div className="remote-inbox">
            <span className="x-eyebrow">Requests from other labs</span>
            {inbox.map((r) => (
              <Permission key={r.jobId} args={r.args as PlanArgs} approver={approver} closed={closed.has(r.jobId)}
                          onClose={(c) => setClosed(r.jobId, c)} from={{ lab: r.from, via: r.via, request: r.request }}
                          needName={() => setNote("Add your name next to the input first, so the approval is signed.")} />
            ))}
          </div>
        )}
        {messages?.length === 0 && !inbox?.length && <Welcome onPick={(t) => setDraft(t)} />}
        {messages?.map((m) => (
          <Message key={m._id} m={m} approver={approver} closed={closed} setClosed={setClosed}
                   needName={() => setNote("Add your name next to the input first, so the approval is signed.")} />
        ))}
        {thinking && <div className="m-bot"><span className="m-who"><span className="x-mark">L</span>Laminar</span>
          <span className="x-muted">Thinking…</span></div>}
        <div ref={bottom} />
      </div>

      <div className="pill-dock">
        {pendingClosed && (
          <div className="pending-pill">
            <i className="x-dot warn" />
            <span><b>Permission pending</b> · {pending.requestText.slice(0, 60)}</span>
            <button className="x-btn sm" onClick={reopen}>Review</button>
          </div>
        )}
        {(note || mic.error) && <div className="pending-pill" style={{ borderColor: "var(--hair)" }}>{note ?? mic.error}</div>}
        <form className="pill" onSubmit={(e) => { e.preventDefault(); submit(draft, "chat"); }}>
          <input value={mic.listening ? mic.interim : draft} readOnly={mic.listening} aria-label="Message the assistant"
                 placeholder={mic.listening ? "Listening…" : pending ? "Ask, or say “approve” / “reject”…" : "Ask the robot to pour, lift, carry…"}
                 onChange={(e) => setDraft(e.target.value)} />
          {canListen && (
            <button type="button" className={`x-icon-btn ${mic.listening ? "rec" : ""}`} aria-label={mic.listening ? "Stop listening" : "Speak"}
                    title={mic.listening ? "Stop listening" : "Speak"} onClick={() => (mic.listening ? mic.stop() : mic.start())}>
              <MicIcon />
            </button>
          )}
          {canSpeak && (
            <button type="button" className={`x-icon-btn ${speakOn === "on" ? "on" : ""}`} aria-label="Read replies aloud"
                    aria-pressed={speakOn === "on"} title="Read replies aloud"
                    onClick={() => { const on = speakOn !== "on"; setSpeakOn(on ? "on" : "off"); if (!on) window.speechSynthesis.cancel(); }}>
              <SpeakerIcon />
            </button>
          )}
          <button type="submit" className="pill-send" aria-label="Send" disabled={mic.listening || !draft.trim()}><ArrowUp /></button>
        </form>
        <label className="pill-meta">APPROVING AS
          <input value={approver} placeholder="your name" onChange={(e) => setApprover(e.target.value)} />
        </label>
      </div>
    </div>
  );
}

function planSentence(a: PlanArgs): string {
  if (!a.pours.length) return `Plan: ${a.steps.map((s) => s.label).join(", then ")}. Say approve or reject.`;
  return `Plan: ${a.pours.map((p) => `${p.ml} millilitres of ${p.vialName} into ${p.containerName}`).join(", then ")}. Say approve or reject.`;
}

function Welcome({ onPick }: { onPick: (t: string) => void }) {
  const st = useStation();
  const why = notReadyReason(st);
  const ex = ["What's on the bench?", "Pour 1 mL of vial A into container 2", "Lift the arm 50 mm",
              "1.5 mL of vial B into container 3, then 1 mL into container 4", "Why did the last run stop?"];
  return (
    <div className="lab2-empty">
      <span className="x-eyebrow">Lab assistant</span>
      <h1 className="x-h1">Tell the robot what to pour.<br /><mark>Nothing moves until you approve.</mark></h1>
      <p className="x-lede" style={{ margin: "14px auto 0" }}>
        Type or speak. The assistant checks every plan against the bench and asks for your permission before the arm moves.
      </p>
      {why && <p className="x-small" style={{ marginTop: 12 }}><span className="x-tag warn">not ready</span> <span className="x-muted">{why}</span></p>}
      <div className="chips">{ex.map((t) => <button key={t} className="chip2" onClick={() => onPick(t)}>{t}</button>)}</div>
    </div>
  );
}

function Message({ m, approver, closed, setClosed, needName }: { m: Msg; approver: string; closed: Set<string>;
                   setClosed: (id: string, c: boolean) => void; needName: () => void }) {
  if (m.tool?.name === "plan") {
    const a = m.tool.args as PlanArgs;
    return <Permission args={a} approver={approver} closed={closed.has(a.jobId)} onClose={(c) => setClosed(a.jobId, c)} needName={needName} />;
  }
  if (m.tool?.name === "bench_setup") return <SetupCard r={m.tool.args as SetupArgs} />;
  if (m.role === "tool") return <div className="m-sys">{m.text}</div>;
  if (m.role === "user") return <div className="m-user">{m.text}</div>;
  const summary = m.tool?.name === "run_summary" ? (m.tool.args as { status: string }).status : null;
  return (
    <div className="m-bot">
      <span className="m-who"><span className="x-mark">L</span>Laminar
        {summary && <span className={`x-tag ${summary === "done" ? "ok" : "bad"}`}>{summary === "done" ? "run finished" : "run stopped"}</span>}
      </span>
      <div className="m-text">{m.text}</div>
    </div>
  );
}

/** The plan as a permission request. × closes it; if no one decided, the pending pill keeps it in view. */
type From = { lab: string; via: string; request: string };

function Permission({ args, approver, closed, onClose, needName, from }: { args: PlanArgs; approver: string; closed: boolean;
                     onClose: (closed: boolean) => void; needName: () => void; from?: From }) {
  const job = useQuery(api.jobs.get, { jobId: args.jobId });
  const rows = useQuery(api.steps.forJob, { jobId: args.jobId });
  const approve = useMutation(api.jobs.approve);
  const reject = useMutation(api.jobs.reject);
  const blocked = notReadyReason(useStation());
  const [showSteps, setShowSteps] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const status = job?.status ?? "awaiting_approval";
  const waiting = status === "awaiting_approval";
  const replaced = status === "rejected" && job?.failReason === "replaced by a newer plan";
  const byIdx = useMemo(() => new Map((rows ?? []).map((s) => [Number(s.name.split(":")[0]), s])), [rows]);
  const running = ["approved", "running", "pouring"].includes(status);

  if (closed && waiting) return <div className="m-sys" id={`perm-${args.jobId}`}>permission request closed · still waiting</div>;
  if (closed && !waiting) return <div className="m-sys">{replaced ? "plan replaced" : `plan ${STATUS_WORDS[status]?.toLowerCase() ?? status}`}</div>;

  async function act(kind: "approve" | "reject") {
    if (!approver.trim()) { needName(); return; }
    setBusy(true); setErr(null);
    try {
      if (kind === "approve") await approve({ jobId: args.jobId, approvedBy: approver.trim(), via: "button" });
      else await reject({ jobId: args.jobId, approvedBy: approver.trim(), via: "button" });
    } catch (e) { setErr(errText(e)); } finally { setBusy(false); }
  }

  const tag = waiting ? <span className="x-tag warn">waiting for you</span>
    : replaced ? <span className="x-tag">replaced</span>
    : running ? <span className="x-tag live">{STATUS_WORDS[status]}</span>
    : status === "done" ? <span className="x-tag ok">done</span>
    : status === "rejected" ? <span className="x-tag">rejected</span>
    : <span className="x-tag bad">{STATUS_WORDS[status] ?? status}</span>;
  const title = args.pours.length
    ? `Run ${args.pours.length} pour${args.pours.length > 1 ? "s" : ""} on the robot?`
    : `Run ${args.steps.length} step${args.steps.length > 1 ? "s" : ""} on the robot?`;

  return (
    <div className={`perm ${waiting ? "waiting" : ""}`} id={`perm-${args.jobId}`}>
      <div className="perm-head">
        <span>{from ? <>Request from <b>{from.lab}</b> <span className="x-muted">· {from.via === "asi-one" ? "via ASI:One" : "via its agent"}</span></> : "Permission request"}</span>
        <span className="x-grow" />
        {tag}
        <button className="x-icon-btn" aria-label="Close" title={waiting ? "Close (stays pending)" : "Close"} onClick={() => onClose(true)}><Close /></button>
      </div>
      <div className="perm-body">
        <div className="perm-title">{title}</div>
        {from && <div className="x-small x-muted">“{from.request}”</div>}
        {args.pours.length > 0 && (
          <div className="perm-pours">
            {args.pours.map((p, i) => (
              <div key={i} className="perm-pour">
                <span className="perm-ml">{p.ml} mL</span>
                <span><b>{p.vialName}</b> <span className="x-muted x-small">vial {p.vialLetter}</span></span>
                <span className="arrow x-muted" aria-hidden>→</span>
                <span><b>{p.containerName}</b> <span className="x-muted x-small num">{p.containerMlNow} → {(p.containerMlNow + p.ml).toFixed(2)} / {p.capacityMl} mL</span></span>
              </div>
            ))}
          </div>
        )}
        {(showSteps || !args.pours.length || running || status === "failed" || status === "estop") ? (
          <ol className="perm-steps">
            {args.steps.map((s, i) => {
              const r = byIdx.get(i);
              const st = !r ? "todo" : r.ok === undefined ? "now" : r.ok ? "ok" : "bad";
              return (
                <li key={i} style={{ flexWrap: "wrap" }}>
                  <span className="n">{String(i + 1).padStart(2, "0")}</span>
                  <span style={{ flex: 1, color: st === "todo" ? "var(--muted)" : undefined }}>{s.label}</span>
                  {s.placeholder && <span className="x-tag warn" title="Not recorded yet: printed only, the arm does nothing">placeholder</span>}
                  {st === "ok" && <span className="x-tag ok">pass</span>}
                  {st === "now" && <span className="x-tag live">now</span>}
                  {st === "bad" && <span className="x-tag bad">fail</span>}
                  {r?.reason && st === "bad" && <div className="why" style={{ flexBasis: "100%" }}>{r.reason}</div>}
                </li>
              );
            })}
          </ol>
        ) : (
          <button className="link2" onClick={() => setShowSteps(true)}>Show the {args.steps.length} robot steps</button>
        )}
        {err && <div className="x-small" style={{ color: "var(--bad)" }}>{err}</div>}
      </div>
      {waiting && (
        <div className="perm-foot">
          <button className="x-btn primary" disabled={busy || !!blocked} title={blocked ?? undefined} onClick={() => act("approve")}>Approve and run</button>
          <button className="x-btn" disabled={busy} onClick={() => act("reject")}>Reject</button>
          <span className="x-grow" />
          <span className="x-small x-muted">{blocked ?? (approver.trim() ? `signing as ${approver.trim()}` : "add your name below to approve")}</span>
        </div>
      )}
      {!waiting && job?.approvedBy && status !== "rejected" && (
        <div className="perm-foot"><span className="x-small x-muted">Approved by {job.approvedBy}{job.approvedVia === "voice" ? " (voice)" : ""}</span></div>
      )}
    </div>
  );
}

type SetupArgs = { containers: { number: number; name: string; contents: string; liquid_color: string | null;
                                 box_fits: boolean; problem: string | null }[]; missed: number; summary: string };

function SetupCard({ r }: { r: SetupArgs }) {
  const bad = r.containers.filter((c) => !c.box_fits);
  return (
    <div className="perm">
      <div className="perm-head">
        <span>Bench set up from the camera</span><span className="x-grow" />
        <span className={`x-tag ${bad.length || r.missed ? "warn" : "ok"}`}>{bad.length || r.missed ? "check calibration" : "looks right"}</span>
      </div>
      <div className="perm-body">
        <div>{r.summary}</div>
        <ol className="perm-steps">
          {r.containers.map((c) => (
            <li key={c.number}>
              <span className="n">{String(c.number).padStart(2, "0")}</span>
              <span style={{ flex: 1 }}><b>{c.name}</b> <span className="x-muted x-small">{c.contents === "liquid" ? (c.liquid_color ?? "liquid") : c.contents}</span></span>
              {c.box_fits ? <span className="x-tag ok">box fits</span> : <span className="x-tag bad" title={c.problem ?? ""}>{c.problem ?? "box off"}</span>}
            </li>
          ))}
        </ol>
        {r.missed > 0 && <div className="x-small" style={{ color: "var(--bad)" }}>{r.missed} container{r.missed === 1 ? " has" : "s have"} no box: recalibrate on the Bench page.</div>}
      </div>
    </div>
  );
}

function MicIcon() {
  return (<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
    <rect x="9" y="3" width="6" height="12" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg>);
}
function SpeakerIcon() {
  return (<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
    <path d="M4 9v6h4l5 4V5L8 9H4z" /><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12" /></svg>);
}
function ArrowUp() {
  return (<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden>
    <path d="M12 19V5M5 12l7-7 7 7" /></svg>);
}
