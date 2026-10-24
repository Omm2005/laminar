import { useMemo } from "react";
import { useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";

type Row = {
  _id: string;
  t: number;
  category: "approval" | "start" | "stop" | "abort" | "result" | "other";
  message: string;
  source: string;
};

function classify(e: { kind: string; message: string; source: string }): Row["category"] {
  const m = e.message.toLowerCase();
  if (e.kind === "estop") return "stop";
  if (m.includes("clear")) return "stop";
  if (m.startsWith("job approved") || m.includes("approved by")) return "approval";
  if (m.startsWith("robot client picked up")) return "start";
  if (m.includes("stalled") || e.kind === "error") return "abort";
  if (m.startsWith("status -> done")) return "result";
  return "other";
}

export default function Audit() {
  const events = useQuery(api.events.recent, { limit: 500 });

  const rows: Row[] = useMemo(() => {
    return (events ?? []).map((e) => ({
      _id: e._id,
      t: e.t,
      category: classify(e),
      message: e.message,
      source: e.source,
    }));
  }, [events]);

  function exportCsv() {
    const header = ["timestamp", "category", "source", "message"].join(",");
    const body = rows.map((r) =>
      [new Date(r.t).toISOString(), r.category, r.source, JSON.stringify(r.message)].join(","),
    ).join("\n");
    const blob = new Blob([header + "\n" + body], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `audit_${new Date().toISOString().replace(/[:.]/g, "-")}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <section className="card">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <div className="card-title" style={{ margin: 0 }}>Audit log</div>
        <button className="btn-primary" onClick={exportCsv} disabled={rows.length === 0}>
          Export CSV
        </button>
      </div>
      {rows.length === 0 ? (
        <div className="empty-text">
          Approvals, starts, stops, aborts and results will show up here once jobs run.
        </div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>When</th>
              <th>What</th>
              <th>Who</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r._id}>
                <td className="mono num">{new Date(r.t).toLocaleString()}</td>
                <td><span className={`pill audit-${r.category}`}>{r.category}</span></td>
                <td className="mono small dim">{r.source}</td>
                <td>{r.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
