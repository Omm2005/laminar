import { useEffect, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";
import { STATUS_WORDS, useStation } from "../station";

/** Bottom-right toasts while something is working: the robot's run (expandable: every step, the live level, STOP)
 *  and bench requests such as saving a calibration. Shown on every page. */

type Step = { name: string; label?: string; placeholder?: boolean };
const RUNNING = ["approved", "running", "pouring"];
const LINGER_MS = 15_000;            // a finished run stays this long

export default function Toasts() {
  return (
    <div className="toasts" aria-live="polite">
      <CommandToast />
      <RunToast />
    </div>
  );
}

function useNow(ms: number) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(id); }, [ms]);
  return now;
}

function RunToast() {
  const job = useQuery(api.jobs.current, {});
  const steps = useQuery(api.steps.forJob, job ? { jobId: job._id } : "skip");
  const levels = useQuery(api.robot.levelsForJob, job ? { jobId: job._id, limit: 400 } : "skip");
  const estop = useMutation(api.control.estop);
  const [open, setOpen] = useState(false);
  const [hidden, setHidden] = useState<string | null>(null);
  const now = useNow(1000);
  if (!job || !job.plan?.length || hidden === job._id) return null;
  const active = RUNNING.includes(job.status);
  const finished = ["done", "failed", "estop", "stalled"].includes(job.status) && job.finishedAt && now - job.finishedAt < LINGER_MS;
  if (!active && !finished) return null;

  const plan = job.plan as Step[];
  const byIdx = new Map((steps ?? []).map((s) => [Number(s.name.split(":")[0]), s]));
  const done = (steps ?? []).filter((s) => s.ok === true).length;
  const cur = steps?.length ? Number(steps[steps.length - 1].name.split(":")[0]) : -1;
  const last = levels?.length ? levels[levels.length - 1] : null;
  const title = active
    ? (job.status === "approved" ? "Waiting for the robot" : plan[cur]?.label ?? "Running")
    : job.status === "done" ? "Run finished" : `Run stopped${job.failReason ? `: ${job.failReason}` : ""}`;
  const tone = active ? "live" : job.status === "done" ? "ok" : "bad";

  return (
    <div className="toast" role="status">
      <button className="toast-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <i className={`x-dot ${tone}`} />
        <span className="t-title">{title}</span>
        <span className="t-count">{done}/{plan.length}</span>
        <Chevron open={open} />
      </button>
      <div className="toast-prog"><i style={{ width: `${(100 * done) / plan.length}%` }} /></div>
      {open && (
        <div className="toast-body">
          {job.status === "pouring" && last && (
            <div><span className="big-ml">{last.ml.toFixed(2)}</span> <span className="x-muted x-small">mL in container {last.channel}</span></div>
          )}
          <ul className="toast-steps">
            {plan.map((s, i) => {
              const r = byIdx.get(i);
              const st = !r ? "todo" : r.ok === undefined ? "now" : r.ok ? "ok" : "bad";
              return (
                <li key={i} className={st === "todo" ? "todo" : ""}>
                  <i className={`x-dot ${st === "ok" ? "ok" : st === "bad" ? "bad" : st === "now" ? "live" : ""}`} />
                  <span style={{ flex: 1 }}>{s.label ?? s.name}</span>
                  {s.placeholder && <span className="x-tag warn">placeholder</span>}
                </li>
              );
            })}
          </ul>
          {job.summary && <div className="x-small x-muted">{job.summary}</div>}
          <div style={{ display: "flex", gap: 8 }}>
            {active && <button className="x-btn sm danger" onClick={() => estop({ source: "web", message: "STOP pressed on the dashboard" })}>Stop</button>}
            {!active && <button className="x-btn sm" onClick={() => setHidden(job._id)}>Dismiss</button>}
            <span className="x-small x-muted" style={{ marginLeft: "auto", alignSelf: "center" }}>
              {STATUS_WORDS[job.status] ?? job.status}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

function CommandToast() {
  const st = useStation();
  const now = useNow(1000);
  const [hidden, setHidden] = useState<string | null>(null);
  const cmd = st && "lastCommand" in st ? st.lastCommand : null;
  if (!cmd || hidden === cmd._id) return null;
  const working = cmd.status === "pending" || cmd.status === "running";
  if (!working && now - cmd.createdAt > LINGER_MS) return null;
  const what = cmd.kind === "set_boxes" ? "Calibration" : cmd.kind === "set_camera" ? "Camera" : cmd.kind;
  return (
    <div className="toast" role="status">
      <div className="toast-head">
        <i className={`x-dot ${working ? "live" : cmd.status === "done" ? "ok" : "bad"}`} />
        <span className="t-title">{working ? `${what}: applying on the bench…` : `${what}: ${cmd.message ?? cmd.status}`}</span>
        {!working && <button className="x-icon-btn" aria-label="Dismiss" onClick={() => setHidden(cmd._id)}><Close /></button>}
      </div>
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden
         style={{ transform: open ? "rotate(180deg)" : "none", transition: "transform .2s", color: "var(--muted)" }}>
      <path d="M6 15l6-6 6 6" />
    </svg>
  );
}

export function Close() {
  return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><path d="M6 6l12 12M18 6L6 18" /></svg>;
}
