import { useEffect, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { Link } from "react-router-dom";
import { api } from "../../convex/_generated/api";

const STALE_MS = 2000;

function vialLetter(n: number): string {
  return String.fromCharCode(64 + Math.max(1, Math.min(26, n)));
}
function jobLine(job?: { targetMl: number; channel: number; vial: number } | null): string {
  if (!job || job.targetMl <= 0) return "No job";
  return `Pour ${job.targetMl.toFixed(1)} mL into channel ${job.channel} from vial ${vialLetter(job.vial)}`;
}
function statusLabel(status: string): string {
  if (status === "awaiting_approval") return "Awaiting approval";
  if (status === "idle") return "Idle";
  if (status === "estop") return "Stopped";
  return status.charAt(0).toUpperCase() + status.slice(1);
}
function stepInWords(
  status: string,
  stepName: string | null | undefined,
  job: { channel: number; vial: number; targetMl: number; plan?: { label?: string }[] } | null,
  poured: number | null,
): string {
  if (status === "awaiting_approval") return "Waiting for approval";
  if (status === "planning")  return "Planning the pour";
  if (status === "queued")    return "In the queue";
  if (status === "approved")  return "Ready to run";
  if (status === "done")      return "Done";
  if (status === "failed")    return "Failed";
  if (status === "stalled")   return "Stalled";
  if (status === "estop")     return "Stopped";
  if (status === "rejected")  return "Rejected, nothing moved";
  if (!job) return "Idle";
  // agent plans (Lab page): currentStep is "<index>:<skill>"; the plan has the words
  const idx = stepName && /^\d+:/.test(stepName) ? Number(stepName.split(":")[0]) : -1;
  if (idx >= 0 && job.plan?.[idx]?.label) return job.plan[idx].label!;
  const V = vialLetter(job.vial);
  const C = job.channel;
  switch (stepName) {
    case "grasp":         return `Grasping vial ${V}`;
    case "lift":          return "Lifting";
    case "traverse":      return `Moving over channel ${C}`;
    case "lower":         return `Lowering over channel ${C}`;
    case "adaptive_pour": return poured == null
      ? `Starting the pour into channel ${C}`
      : `Pouring ${poured.toFixed(1)} / ${job.targetMl.toFixed(1)} mL`;
    case "untilt":        return "Returning to upright";
    case "return_vial":   return `Carrying vial ${V} back`;
    case "home":          return "Returning home";
    default:              return status === "running" ? "Running" : "Standing by";
  }
}

export default function StatusBar() {
  const jobs = useQuery(api.jobs.list, { limit: 1 });
  const activeJob = jobs?.[0] ?? null;
  const telem = useQuery(api.robot.latestTelemetry, {});
  const levels = useQuery(
    api.robot.levelsForJob,
    activeJob ? { jobId: activeJob._id, limit: 400 } : "skip",
  );
  const stopState = useQuery(api.control.stopRequested, {});
  const estop = useMutation(api.control.estop);
  const clearStop = useMutation(api.control.clearStop);

  // tick every 500 ms so the latency / staleness badges don't go stale themselves
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 500);
    return () => clearInterval(id);
  }, []);

  const now = Date.now();
  const lastT = telem?.t ?? 0;
  const sinceMs = lastT ? now - lastT : Infinity;
  const stale = sinceMs > STALE_MS;
  const status = activeJob?.status ?? "idle";
  const stopped = !!stopState?.stop;

  const poured = levels && levels.length >= 1
    ? Math.max(0, (levels[levels.length - 1].ml) - (levels[0].ml))
    : null;

  return (
    <section className="status-bar">
      <div className="sb-left">
        <div className="sb-pill" data-status={status}>{statusLabel(status)}</div>
        <div className="sb-job">{jobLine(activeJob)}</div>
      </div>
      <div className="sb-center">{stepInWords(status, activeJob?.currentStep, activeJob, poured)}</div>
      <div className="sb-right">
        {activeJob?.status === "awaiting_approval" && (
          <Link className="btn-primary" to="/lab">Review the plan</Link>
        )}
        {stopped ? (
          <button className="btn-stop-mini" onClick={() => clearStop({ source: "web" })}>Clear stop</button>
        ) : (
          <button className="btn-stop-mini" onClick={() => estop({ source: "web", message: "manual" })}>Stop</button>
        )}
        {!stale && <span className="live-pill">LIVE · {Math.round(sinceMs)} ms</span>}
      </div>
    </section>
  );
}
