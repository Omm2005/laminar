import { useMemo, useState } from "react";
import { useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";
import type { Doc, Id } from "../../convex/_generated/dataModel";
import { STATUS_WORDS } from "../station";

/** Audit: every plan the assistant made, who approved it, what the robot did step by step, what the camera
 *  measured, and the event trail. Runs on the left, the selected run on the right. */

type Job = Doc<"jobs">;
type ReportStep = { skill: string; status: string; reason?: string; args?: Record<string, unknown>;
                    telemetry?: Record<string, unknown> };
const FILTERS = [
  { key: "all", label: "All" }, { key: "done", label: "Done" }, { key: "stopped", label: "Stopped" },
  { key: "rejected", label: "Rejected" }, { key: "open", label: "Open" },
] as const;

function group(j: Job): (typeof FILTERS)[number]["key"] {
  if (j.status === "done") return "done";
  if (["failed", "estop", "stalled"].includes(j.status)) return "stopped";
  if (j.status === "rejected") return "rejected";
  return "open";
}

function ago(t: number) {
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(t).toLocaleDateString();
}

function statusTag(j: Job) {
  const g = group(j);
  const cls = g === "done" ? "ok" : g === "stopped" ? "bad" : g === "rejected" ? "" : "warn";
  const word = j.status === "rejected" && j.failReason === "replaced by a newer plan" ? "replaced" : STATUS_WORDS[j.status] ?? j.status;
  return <span className={`x-tag ${cls}`}>{word}</span>;
}

export default function Audit() {
  const jobs = useQuery(api.jobs.list, { limit: 200 }) as Job[] | undefined;
  const events = useQuery(api.events.recent, { limit: 500 });
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["key"]>("all");
  const [sel, setSel] = useState<Id<"jobs"> | null>(null);
  const runs = useMemo(() => (jobs ?? []).filter((j) => j.status !== "queued" && (filter === "all" || group(j) === filter)), [jobs, filter]);
  const current = runs.find((j) => j._id === sel) ?? runs[0] ?? null;
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: 0, done: 0, stopped: 0, rejected: 0, open: 0 };
    for (const j of jobs ?? []) { if (j.status === "queued") continue; c.all++; c[group(j)]++; }
    return c;
  }, [jobs]);

  function exportCsv() {
    const rows = [["created", "request", "status", "approved_by", "via", "finished", "fail_reason", "summary"]];
    for (const j of jobs ?? []) {
      rows.push([new Date(j.createdAt).toISOString(), j.requestText, j.status, j.approvedBy ?? "", j.approvedVia ?? "",
                 j.finishedAt ? new Date(j.finishedAt).toISOString() : "", j.failReason ?? "", j.summary ?? ""]);
    }
    const csv = rows.map((r) => r.map((x) => JSON.stringify(String(x))).join(",")).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = `laminar-audit-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
  }

  return (
    <>
      <section className="x-hero x-wash">
        <span className="x-eyebrow">Audit</span>
        <h1 className="x-h1">Every plan, every approval.<br /><mark>Exactly what the robot did.</mark></h1>
        <p className="x-lede">Who asked, who approved, each step's result and what the camera measured, kept for every run.</p>
      </section>
      <section className="x-section">
        <div className="x-panel">
          <div className="x-panel-head">
            <div className="x-tabs" role="tablist">
              {FILTERS.map((f) => (
                <button key={f.key} role="tab" aria-selected={filter === f.key} className={filter === f.key ? "on" : ""}
                        onClick={() => { setFilter(f.key); setSel(null); }}>{f.label} {counts[f.key] ? `· ${counts[f.key]}` : ""}</button>
              ))}
            </div>
            <span className="x-grow" />
            <button className="x-btn sm" onClick={exportCsv} disabled={!jobs?.length}>Export CSV</button>
          </div>
          {runs.length === 0 ? (
            <div className="x-panel-body x-muted">{jobs === undefined ? "Loading…" : "No runs yet. Ask for something in the Lab."}</div>
          ) : (
            <div className="audit">
              <div className="audit-list" role="listbox" aria-label="Runs">
                {runs.map((j) => (
                  <button key={j._id} role="option" aria-selected={current?._id === j._id}
                          className={`audit-row ${current?._id === j._id ? "on" : ""}`} onClick={() => setSel(j._id)}>
                    <div className="r1"><span className="req">{j.requestText}</span>{statusTag(j)}</div>
                    <div className="r2"><span>{ago(j.createdAt)}</span>{j.approvedBy && <span>approved by {j.approvedBy}</span>}
                      <span>{(j.plan ?? []).length} steps</span></div>
                  </button>
                ))}
              </div>
              {current && <Detail job={current} events={(events ?? []).filter((e) => e.jobId === current._id)} />}
            </div>
          )}
        </div>
      </section>
    </>
  );
}

function Detail({ job, events }: { job: Job; events: Doc<"events">[] }) {
  const rows = useQuery(api.steps.forJob, { jobId: job._id });
  const plan = (job.plan ?? []) as { name: string; label?: string; placeholder?: boolean }[];
  const report = (job.report?.steps ?? []) as ReportStep[];
  const byIdx = new Map((rows ?? []).map((s) => [Number(s.name.split(":")[0]), s]));
  const dur = job.finishedAt ? `${Math.round((job.finishedAt - job.createdAt) / 1000)} s` : "—";
  return (
    <div className="audit-detail">
      <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
        <h2 className="x-h2" style={{ flex: 1 }}>{job.requestText}</h2>{statusTag(job)}
      </div>
      <div className="kv">
        <div><div className="k">Asked</div><div className="v">{new Date(job.createdAt).toLocaleString()}</div></div>
        <div><div className="k">Approved by</div><div className="v">{job.approvedBy ? `${job.approvedBy}${job.approvedVia ? ` · ${job.approvedVia}` : ""}` : "—"}</div></div>
        <div><div className="k">Took</div><div className="v mono">{dur}</div></div>
        <div><div className="k">Measured</div><div className="v mono">
          {job.report?.results?.measured_ml != null ? `${job.report.results.measured_ml} mL (±${Math.abs(job.report.results.error_ml ?? 0).toFixed(2)})` : "—"}</div></div>
      </div>
      {job.summary && <div className="summary-box"><span className="x-eyebrow" style={{ marginBottom: 6, display: "flex" }}>Assistant</span>{job.summary}</div>}
      {job.failReason && !job.summary && <div className="summary-box" style={{ color: "var(--bad)" }}>{job.failReason}</div>}
      <div className="x-panel" style={{ boxShadow: "none" }}>
        <div className="x-panel-head"><span>Steps</span></div>
        <table className="x-table">
          <tbody>
            {plan.map((s, i) => {
              const r = byIdx.get(i);
              const rep = report[i];
              const status = rep?.status ?? (!r ? "pending" : r.ok === undefined ? "running" : r.ok ? "ok" : "failed");
              const t = rep?.telemetry ?? {};
              const tag = status === "ok" ? <span className="x-tag ok">pass</span> : status === "failed" ? <span className="x-tag bad">fail</span>
                : status === "running" ? <span className="x-tag live">running</span> : status === "skipped" ? <span className="x-tag">skipped</span>
                : <span className="x-tag">—</span>;
              return (
                <tr key={i}>
                  <td className="mono x-muted" style={{ width: 36 }}>{String(i + 1).padStart(2, "0")}</td>
                  <td>{s.label ?? s.name}{(rep?.reason || r?.reason) && status !== "ok" &&
                    <div className="x-small" style={{ color: status === "failed" ? "var(--bad)" : "var(--muted)" }}>{rep?.reason || r?.reason}</div>}</td>
                  <td className="mono x-small x-muted" style={{ width: 150 }}>
                    {t.measured_ml != null ? `${t.measured_ml} mL` : t.dispensed_ml != null ? `${t.dispensed_ml} mL poured` : t.moves_s != null ? `${t.moves_s} s` : ""}
                  </td>
                  <td style={{ width: 110 }}>{s.placeholder ? <span className="x-tag warn">placeholder</span> : tag}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {events.length > 0 && (
        <div>
          <span className="x-eyebrow">Event trail</span>
          <ul className="timeline" style={{ marginTop: 12 }}>
            {[...events].reverse().map((e) => (
              <li key={e._id} className={e.kind === "error" || e.kind === "estop" ? "err" : ""}>
                <span className="ts">{new Date(e.t).toLocaleTimeString()}</span>{e.message}
                <span className="x-muted x-small"> · {e.source}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
