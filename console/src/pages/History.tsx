import { useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";
import type { Id } from "../../convex/_generated/dataModel";

export default function History() {
  const pours = useQuery(api.pours.recent, { limit: 50 });

  return (
    <div className="card">
      <h2>Pours</h2>
      {!pours ? <p className="mono">loading…</p>
        : pours.length === 0 ? <p className="mono">no pours yet</p>
          : (
            <table className="table">
              <thead>
                <tr>
                  <th>when</th><th>target</th><th>poured</th><th>error</th>
                  <th>onset / pour °</th><th>mL/s</th><th>snapshots</th>
                </tr>
              </thead>
              <tbody>
                {pours.map((p) => (
                  <tr key={p._id}>
                    <td className="mono">{new Date(p.createdAt).toLocaleString()}</td>
                    <td className="mono">{p.targetMl.toFixed(2)} mL</td>
                    <td className="mono">{p.pouredMl.toFixed(2)} mL</td>
                    <td className="mono" style={{
                      color: Math.abs(p.errorMl) < 0.3 ? "var(--ok)"
                        : Math.abs(p.errorMl) < 1.0 ? "var(--warn)" : "var(--danger)",
                    }}>
                      {p.errorMl >= 0 ? "+" : ""}{p.errorMl.toFixed(2)}
                    </td>
                    <td className="mono">{p.onsetDeg.toFixed(0)} / {p.pourDeg.toFixed(0)}</td>
                    <td className="mono">{p.measuredMlPerS.toFixed(2)}</td>
                    <td>
                      <Snap id={p.snapshotBefore} label="before" />
                      <Snap id={p.snapshotAfter} label="after" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
    </div>
  );
}

function Snap({ id, label }: { id?: Id<"_storage">; label: string }) {
  const url = useQuery(api.storage.getUrl, id ? { storageId: id } : "skip");
  if (!id) return <span className="mono" style={{ marginRight: 6 }}>—</span>;
  if (!url) return <span className="mono" style={{ marginRight: 6 }}>{label}…</span>;
  return (
    <a href={url} target="_blank" rel="noreferrer" style={{ marginRight: 6 }}>
      <img src={url} alt={label} style={{ width: 56, height: 42, objectFit: "cover", borderRadius: 4, border: "1px solid var(--border)" }} />
    </a>
  );
}
