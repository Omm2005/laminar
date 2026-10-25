import { useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";
import type { Id } from "../../convex/_generated/dataModel";

const EXAMPLES = [
  "pour 2 ml into channel 3 from vial 1",
  "pour 4 ml into channel 7",
  "pour half of channel 2 from vial 1",
  "pour quarter of channel 12 from vial 4",
];

export default function Jobs() {
  const jobs = useQuery(api.jobs.list, { limit: 30 });
  const submit = useMutation(api.requests.submit);
  const approve = useMutation(api.jobs.approve);
  const reject = useMutation(api.jobs.reject);

  const [text, setText] = useState(EXAMPLES[0]);
  const [who, setWho] = useState("hema");
  const [selected, setSelected] = useState<Id<"jobs"> | null>(null);
  const selectedJob = jobs?.find((j) => j._id === selected) ?? null;

  async function onSubmit() {
    if (!text.trim()) return;
    const id = await submit({ requestText: text.trim(), requestedBy: who });
    setSelected(id);
  }

  return (
    <>
      <section className="surface">
        <div className="section-title">submit a pour</div>
        <textarea
          rows={2}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder='e.g. "pour 2 ml into channel 3 from vial 1"'
        />
        <div className="row" style={{ marginTop: 8 }}>
          {EXAMPLES.map((ex) => (
            <button key={ex} className="chip" onClick={() => setText(ex)}>{ex}</button>
          ))}
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <input
            style={{ maxWidth: 160 }}
            value={who}
            onChange={(e) => setWho(e.target.value)}
            placeholder="your name"
          />
          <button className="btn-primary" onClick={onSubmit}>Submit</button>
        </div>
      </section>

      <div className="live-grid">
        <section className="surface">
          <div className="section-title">recent jobs</div>
          {!jobs ? <div className="small dim">loading…</div>
            : jobs.length === 0 ? <div className="small dim">no jobs yet — submit one above</div>
              : (
                <ul className="jobs-list">
                  {jobs.map((j) => (
                    <li key={j._id}
                        className={selected === j._id ? "selected" : ""}
                        onClick={() => setSelected(j._id)}>
                      <div className="jobs-row-top">
                        <span className="mono small">{fmtAgo(j.createdAt)}</span>
                        <span className={`pill ${j.status}`}>{j.status.replace("_"," ")}</span>
                      </div>
                      <div className="jobs-row-body">{j.requestText}</div>
                      {j.targetMl > 0 && (
                        <div className="small dim mono">
                          {j.targetMl.toFixed(1)} mL → channel {j.channel} / vial {j.vial}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
        </section>

        <aside className="live-side">
          <section className="surface">
            <div className="section-title">plan</div>
            {!selectedJob ? (
              <div className="small dim">select a job to see its live stepper</div>
            ) : (
              <PlanStepper
                jobId={selectedJob._id}
                plan={selectedJob.plan ?? []}
                status={selectedJob.status}
                currentStep={selectedJob.currentStep}
              />
            )}
          </section>
          {selectedJob?.status === "awaiting_approval" && (
            <section className="surface">
              <div className="section-title">approval</div>
              <div className="row">
                <button className="btn-primary" onClick={() => approve({ jobId: selectedJob._id, approvedBy: who })}>Approve</button>
                <button className="btn-ghost" onClick={() => reject({ jobId: selectedJob._id, approvedBy: who, reason: "rejected from console" })}>Reject</button>
              </div>
            </section>
          )}
          {selectedJob?.failReason && (
            <section className="surface">
              <div className="section-title">failure</div>
              <div className="small" style={{ color: "var(--bad)" }}>{selectedJob.failReason}</div>
            </section>
          )}
        </aside>
      </div>
    </>
  );
}

function PlanStepper({ jobId, plan, status, currentStep }: {
  jobId: Id<"jobs">;
  plan: { name: string; args?: unknown }[];
  status: string;
  currentStep?: string;
}) {
  const steps = useQuery(api.steps.forJob, { jobId });
  if (!plan.length) {
    return <div className="small dim">planner is still working…</div>;
  }
  // Map planned step index -> most recent step row of that name (if any)
  const stepRowsByName = new Map<string, { ok?: boolean; reason?: string }>();
  for (const s of steps ?? []) {
    stepRowsByName.set(s.name, { ok: s.ok, reason: s.reason });
  }
  return (
    <ol className="stepper">
      {plan.map((p, i) => {
        const row = stepRowsByName.get(p.name);
        const isCurrent = currentStep === p.name;
        let state: "pending" | "current" | "done" | "fail" = "pending";
        if (row?.ok === true) state = "done";
        else if (row?.ok === false) state = "fail";
        else if (isCurrent) state = "current";
        else if (status === "done") state = "done";
        return (
          <li key={i} className={`step step-${state}`}>
            <span className="step-dot" />
            <div>
              <div><code>{p.name}</code>
                {p.args && Object.keys(p.args as Record<string, unknown>).length > 0 ? (
                  <span className="small dim mono"> {JSON.stringify(p.args)}</span>
                ) : null}
              </div>
              {row?.reason && (
                <div className="small" style={{ color: "var(--bad)" }}>{row.reason}</div>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function fmtAgo(ts: number) {
  const dt = (Date.now() - ts) / 1000;
  if (dt < 60) return `${Math.floor(dt)}s`;
  if (dt < 3600) return `${Math.floor(dt / 60)}m`;
  if (dt < 86400) return `${Math.floor(dt / 3600)}h`;
  return new Date(ts).toLocaleDateString();
}
