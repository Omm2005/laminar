import { useEffect, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../convex/_generated/api";
import type { Id } from "../convex/_generated/dataModel";
import { DYE, STANDS, ago } from "./ui";

type Tube = "A" | "B" | "C" | "D";
type Row = { tube: Tube; well: number; ml: number };
const LIQUID: Record<Tube, string> = { A: "Red · sample", B: "Blue · reagent 1", C: "Yellow · reagent 2", D: "Green · reagent 3" };
const STANDARD: Row[] = [
  { tube: "A", well: 1, ml: 1 }, { tube: "A", well: 2, ml: 1 }, { tube: "A", well: 3, ml: 1 }, { tube: "A", well: 4, ml: 1 },
  { tube: "B", well: 1, ml: 0.5 }, { tube: "C", well: 2, ml: 0.5 }, { tube: "D", well: 3, ml: 0.5 },
];

/** Parses "tube,well,ml" CSV (header optional). Accepts liquid names too: red/blue/yellow/green. */
function parseCsv(text: string): Row[] {
  const alias: Record<string, Tube> = { a: "A", b: "B", c: "C", d: "D", red: "A", blue: "B", yellow: "C", green: "D" };
  const rows: Row[] = [];
  for (const line of text.split(/\r?\n/)) {
    const cells = line.split(/[,;\t]/).map((c) => c.trim().toLowerCase());
    if (cells.length < 3 || !alias[cells[0]]) continue;
    const well = parseInt(cells[1].replace(/\D/g, "")), ml = parseFloat(cells[2]);
    if (Number.isFinite(well) && Number.isFinite(ml)) rows.push({ tube: alias[cells[0]], well, ml });
  }
  if (!rows.length) throw new Error("No rows found. Use one pour per line: tube,well,ml (for example A,1,1.0).");
  return rows;
}

export default function Builder({ name, onSent }: { name: string; onSent: () => void }) {
  const [search, setSearch] = useState("");
  const list = useQuery(api.protocols.list, { search });
  const [id, setId] = useState<Id<"protocols"> | null>(null);
  const current = useQuery(api.protocols.get, id ? { id } : "skip");
  const [title, setTitle] = useState("Standard assay");
  const [rows, setRows] = useState<Row[]>(STANDARD);
  const [file, setFile] = useState<{ id: Id<"_storage">; name: string } | null>(null);
  const [error, setError] = useState("");
  const check = useQuery(api.protocols.validate, { rows });
  const save = useMutation(api.protocols.save);
  const upload = useMutation(api.protocols.generateUploadUrl);
  const toRun = useMutation(api.protocols.toRun);

  useEffect(() => { if (current) { setTitle(current.name); setRows(current.rows); setFile(null); } }, [current?._id, current?.version]); // eslint-disable-line react-hooks/exhaustive-deps

  const perWell = [1, 2, 3, 4].map((w) => rows.filter((r) => r.well === w).reduce((s, r) => s + r.ml, 0));
  const perTube = (["A", "B", "C", "D"] as Tube[]).map((t) => rows.filter((r) => r.tube === t).reduce((s, r) => s + r.ml, 0));
  const set = (i: number, patch: Partial<Row>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const act = async (f: () => Promise<unknown>) => { setError(""); try { await f(); } catch (e) { setError(String((e as Error).message).replace(/^.*Uncaught Error: /s, "").split("\n")[0]); } };

  const importCsv = (f: File) => act(async () => {
    const parsed = parseCsv(await f.text());
    const url = await upload({});
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": f.type || "text/csv" }, body: f });
    const { storageId } = await res.json();
    setRows(parsed); setTitle(f.name.replace(/\.[^.]+$/, "")); setFile({ id: storageId, name: f.name }); setId(null);
  });
  const persist = async () => {
    const saved = await save({ id: id ?? undefined, name: title, rows, by: name, sourceFile: file?.id, sourceName: file?.name });
    setId(saved);
    return saved;
  };

  return (
    <div className="run-grid">
      <div className="stack" style={{ gap: 16 }}>
        <div className="panel stack" style={{ gap: 14 }}>
          <div className="between">
            <label className="field" htmlFor="ptitle" style={{ flex: 1, minWidth: 0 }}>
              <span className="label">Protocol{current ? ` · v${current.version} · saved ${ago(current.updatedAt)}` : " · not saved"}</span>
              <input id="ptitle" className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
            </label>
          </div>
          <div className="tbl">
            <table>
              <thead><tr><th>Tube · liquid</th><th>Well</th><th className="num">Target mL</th><th>Well fill</th><th /></tr></thead>
              <tbody>
                {rows.map((r, i) => {
                  const fill = Math.min(1, perWell[r.well - 1] / 3);
                  return (
                    <tr key={i}>
                      <td>
                        <select className="input" style={{ padding: "5px 8px" }} value={r.tube} onChange={(e) => set(i, { tube: e.target.value as Tube })} aria-label="Tube">
                          {(["A", "B", "C", "D"] as Tube[]).map((t) => <option key={t} value={t}>{t} · {LIQUID[t]}</option>)}
                        </select>
                      </td>
                      <td>
                        <select className="input" style={{ padding: "5px 8px" }} value={r.well} onChange={(e) => set(i, { well: +e.target.value })} aria-label="Well">
                          {[1, 2, 3, 4].map((w) => <option key={w} value={w}>{w}{w === 4 ? " · control" : ""}</option>)}
                        </select>
                      </td>
                      <td className="num"><input className="input num" style={{ width: 80, padding: "5px 8px", textAlign: "right" }} type="number" step="0.1" min="0.2" max="2" value={r.ml} onChange={(e) => set(i, { ml: +e.target.value })} aria-label="Target mL" /></td>
                      <td style={{ minWidth: 110 }}>
                        <div style={{ height: 8, borderRadius: 4, background: "var(--glass)", overflow: "hidden" }}>
                          <div style={{ width: `${fill * 100}%`, height: "100%", background: fill > 0.8 ? "var(--warn)" : DYE[r.tube] }} />
                        </div>
                        <span className="muted" style={{ fontSize: "0.75rem" }}>{perWell[r.well - 1].toFixed(1)} / 3.0 mL</span>
                      </td>
                      <td><button className="btn small" onClick={() => setRows(rows.filter((_, j) => j !== i))} aria-label="Remove row">✕</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="row">
            <button className="btn small" onClick={() => setRows([...rows, { tube: "A", well: 1, ml: 1 }])}>Add pour</button>
            <button className="btn small" onClick={() => { setRows(STANDARD); setTitle("Standard assay"); setId(null); }}>Standard assay</button>
            <label className="btn small" htmlFor="csv" style={{ cursor: "pointer" }}>Import CSV</label>
            <input id="csv" type="file" accept=".csv,text/csv,text/plain" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) void importCsv(f); e.target.value = ""; }} />
            {file && <span className="chip">from {file.name} · stored in Convex</span>}
            {current?.sourceUrl && !file && <a className="chip" href={current.sourceUrl} target="_blank" rel="noreferrer">source: {current.sourceName}</a>}
          </div>
          {error && <p className="err" role="alert">{error}</p>}
        </div>

        <div className="panel stack" style={{ gap: 12 }}>
          <div className="between">
            <span className="label">Live check against the bench</span>
            <span className="chip">{check ? `${check.moves} moves · about ${Math.floor(check.seconds / 60)}m ${check.seconds % 60}s` : "…"}</span>
          </div>
          {check && (check.errors.length === 0
            ? <p className="notice ok">Every rule passes: rack order, tube volumes, pour limits, well capacity, control well, sample first.</p>
            : <div className="notice bad"><ul>{check.errors.map((e) => <li key={e}>{e}</li>)}</ul></div>)}
          <div className="tiles">
            {(["A", "B", "C", "D"] as Tube[]).map((t, i) => (
              <div key={t}><span className="label"><span className="dot" style={{ background: DYE[t] }} /> {t} · {STANDS[t]}</span><b>{perTube[i].toFixed(1)}</b><small>mL needed</small></div>
            ))}
          </div>
          <div className="between">
            <button className="btn" disabled={!name.trim()} onClick={() => act(persist)}>{id ? "Save new version" : "Save protocol"}</button>
            <button className="btn primary" disabled={!name.trim() || !check || check.errors.length > 0} onClick={() => act(async () => { const pid = await persist(); await toRun({ id: pid, by: name }); onSent(); })}>
              {name.trim() ? "Send to bench for approval" : "Enter your name first"}
            </button>
          </div>
          <p className="cap">Sending creates a run that still needs a named approval on the Run tab. The checker runs again at approval time.</p>
        </div>
      </div>

      <div className="panel">
        <label className="field" htmlFor="psearch">
          <span className="label">Saved protocols</span>
          <input id="psearch" className="input" placeholder="Search by name" value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
        <div style={{ marginTop: 8 }}>
          {list?.length === 0 && <p className="muted" style={{ marginTop: 8 }}>{search ? "No match." : "None saved yet."}</p>}
          {list?.map((p) => (
            <button key={p._id} className="list-btn" aria-current={p._id === id} onClick={() => setId(p._id)}>
              <span style={{ minWidth: 0 }}>{p.name} <span className="muted">· v{p.version} · {p.createdBy}</span></span>
              <span className="row" style={{ gap: 3 }}>{[...new Set(p.rows.map((r) => r.tube))].map((t) => <span key={t} className="dot" style={{ background: DYE[t] }} />)}</span>
            </button>
          ))}
        </div>
        <p className="cap">CSV format: one pour per line, <code>tube,well,ml</code>. Liquid names work too: <code>red,1,1.0</code>.</p>
      </div>
    </div>
  );
}
