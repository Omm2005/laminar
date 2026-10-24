import { useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";
import type { Id } from "../../convex/_generated/dataModel";

export default function Jobs() {
  const jobs = useQuery(api.jobs.list, { limit: 30 });
  const submit = useMutation(api.requests.submit);
  const approve = useMutation(api.jobs.approve);
  const reject = useMutation(api.jobs.reject);

  const [text, setText] = useState("pour 4 ml into channel 3 from vial 1");
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
      <div className="card">
        <h2>Submit a pour</h2>
        <textarea
          rows={2}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder='e.g. "pour half of channel 3 from vial 1"'
        />
        <div className="row" style={{ marginTop: 8, alignItems: "center" }}>
          <input
            style={{ maxWidth: 180 }}
            value={who}
            onChange={(e) => setWho(e.target.value)}
            placeholder="your name"
          />
          <button className="primary" onClick={onSubmit}>Submit</button>
        </div>
      </div>

      <div className="row">
        <div className="col" style={{ flex: "2 1 420px" }}>
          <div className="card">
            <h2>Recent jobs</h2>
            {!jobs ? <p className="mono">loading…</p>
              : jobs.length === 0 ? <p className="mono">no jobs yet</p>
                : (
                  <table className="table">
                    <thead>
                      <tr>
                        <th>when</th><th>request</th><th>→</th><th>status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {jobs.map((j) => (
                        <tr
                          key={j._id}
                          style={{
                            cursor: "pointer",
                            background: selected === j._id ? "rgba(125,211,252,0.08)" : undefined,
                          }}
                          onClick={() => setSelected(j._id)}
                        >
                          <td className="mono">{fmtAgo(j.createdAt)}</td>
                          <td>{j.requestText}</td>
                          <td className="mono">
                            {j.targetMl > 0 ? `${j.targetMl.toFixed(1)} mL → ch${j.channel} / v${j.vial}` : "—"}
                          </td>
                          <td><span className={`pill ${j.status}`}>{j.status}</span></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
          </div>
        </div>

        <div className="col" style={{ flex: "1 1 320px" }}>
          <div className="card">
            <h2>Plan</h2>
            {!selectedJob ? <p className="mono">select a job to see its plan</p> : (
              <>
                <p className="mono">{selectedJob.requestText}</p>
                <p className="mono">
                  status: <span className={`pill ${selectedJob.status}`}>{selectedJob.status}</span>
                  {selectedJob.approvedBy ? ` · approved by ${selectedJob.approvedBy}` : ""}
                </p>
                {selectedJob.failReason && (
                  <p style={{ color: "var(--danger)" }}>{selectedJob.failReason}</p>
                )}
                {selectedJob.plan && selectedJob.plan.length > 0 ? (
                  <ol style={{ paddingLeft: 18, margin: "8px 0" }}>
                    {selectedJob.plan.map((s, i) => (
                      <li key={i}>
                        <code>{s.name}</code>
                        {s.args && Object.keys(s.args as object).length > 0 && (
                          <span className="mono"> {JSON.stringify(s.args)}</span>
                        )}
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="mono">no plan yet</p>
                )}
                {selectedJob.status === "awaiting_approval" && (
                  <div className="row" style={{ marginTop: 10 }}>
                    <button className="primary" onClick={() =>
                      approve({ jobId: selectedJob._id, approvedBy: who })}>Approve</button>
                    <button className="ghost" onClick={() =>
                      reject({ jobId: selectedJob._id, approvedBy: who, reason: "rejected from console" })}>Reject</button>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

function fmtAgo(ts: number) {
  const dt = (Date.now() - ts) / 1000;
  if (dt < 60) return `${Math.floor(dt)}s`;
  if (dt < 3600) return `${Math.floor(dt / 60)}m`;
  if (dt < 86400) return `${Math.floor(dt / 3600)}h`;
  return new Date(ts).toLocaleDateString();
}
