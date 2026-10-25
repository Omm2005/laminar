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

/** The segmented fill-after bar: shows every pour already assigned to that well, stacked by liquid colour. */
function FillAfter({ rows, upToIndex, well, cap = 3 }: { rows: Row[]; upToIndex: number; well: number; cap?: number }) {
  const segs = rows.slice(0, upToIndex + 1).filter((r) => r.well === well);
  const total = segs.reduce((s, r) => s + r.ml, 0);
  return (
    <div>
      <div className="fill-bar">
        {segs.map((r, i) => (
          <span key={i} style={{ width: `${Math.min(1, r.ml / cap) * 100}%`, background: DYE[r.tube], display: "inline-block", height: "100%" }} />
        ))}
      </div>
      <div className="fill-cap">{total.toFixed(1)} / {cap.toFixed(1)} mL</div>
    </div>
  );
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

  const savedPill = current ? `v${current.version} · saved ${ago(current.updatedAt)}` : "not saved";
  const minutes = check ? Math.floor(check.seconds / 60) : 0;
  const seconds = check ? check.seconds % 60 : 0;

  return (
    <div className="builder-grid">
      <div className="builder-main">
        <section className="card">
          <div className="builder-head">
            <span className="label">Protocol</span>
            <span className={`chip${current ? " ok" : ""}`}>{savedPill}</span>
            <label className="sr-only" htmlFor="ptitle">Protocol name</label>
            <input id="ptitle" className="input" style={{ flex: "1 1 220px", minWidth: 180 }} value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div className="tbl" style={{ marginTop: 14 }}>
            <table className="pour-table">
              <thead>
                <tr>
                  <th scope="col" style={{ width: 36 }}>#</th>
                  <th scope="col">Tube</th>
                  <th scope="col">Liquid</th>
                  <th scope="col">Well</th>
                  <th scope="col" className="num">Target mL</th>
                  <th scope="col" style={{ minWidth: 140 }}>Well fill after</th>
                  <th scope="col" style={{ width: 32 }} aria-label="Remove"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i}>
                    <td className="mono" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>{i + 1}</td>
                    <td>
                      <label className="liquid-pill">
                        <span className="dot" style={{ background: DYE[r.tube] }} />
                        <select value={r.tube} onChange={(e) => set(i, { tube: e.target.value as Tube })} aria-label="Tube">
                          {(["A", "B", "C", "D"] as Tube[]).map((t) => <option key={t} value={t}>{t}</option>)}
                        </select>
                      </label>
                    </td>
                    <td style={{ fontSize: 13, color: "var(--ink-2)" }}>{LIQUID[r.tube]}</td>
                    <td>
                      <label className="liquid-pill" style={{ paddingLeft: 10 }}>
                        <select value={r.well} onChange={(e) => set(i, { well: +e.target.value })} aria-label="Well">
                          {[1, 2, 3, 4].map((w) => <option key={w} value={w}>{w}{w === 4 ? " · control" : ""}</option>)}
                        </select>
                      </label>
                    </td>
                    <td className="num">
                      <input className="input num" style={{ width: 86, textAlign: "right", padding: "7px 10px" }}
                        type="number" step="0.1" min="0.2" max="2" value={r.ml}
                        onChange={(e) => set(i, { ml: +e.target.value })} aria-label="Target mL" />
                    </td>
                    <td style={{ minWidth: 140 }}>
                      <FillAfter rows={rows} upToIndex={i} well={r.well} />
                    </td>
                    <td>
                      <button type="button" className="circle-x" onClick={() => setRows(rows.filter((_, j) => j !== i))} aria-label="Remove row">×</button>
                    </td>
                  </tr>
                ))}
                {rows.length === 0 && <tr><td colSpan={7} className="muted" style={{ padding: 20 }}>Add a pour, start from the standard assay, or import a CSV.</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="row" style={{ marginTop: 14, flexWrap: "wrap", gap: 10, alignItems: "center" }}>
            <button type="button" className="btn tertiary" onClick={() => setRows([...rows, { tube: "A", well: 1, ml: 1 }])}>Add pour</button>
            <button type="button" className="btn tertiary" onClick={() => { setRows(STANDARD); setTitle("Standard assay"); setId(null); }}>Standard assay</button>
            <label className="btn tertiary" htmlFor="csv" style={{ cursor: "pointer" }}>Import CSV</label>
            <input id="csv" type="file" accept=".csv,text/csv,text/plain" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) void importCsv(f); e.target.value = ""; }} />
            <span className="t-label" style={{ color: "var(--ink-3)", flex: "1 1 200px" }}>tube,well,ml · liquid names work too: red,1,1.0</span>
            {file && <span className="chip">from {file.name}</span>}
            {current?.sourceUrl && !file && <a className="chip" href={current.sourceUrl} target="_blank" rel="noreferrer">source: {current.sourceName}</a>}
            <button type="button" className="btn primary reagent" disabled={!name.trim()} onClick={() => act(persist)}>{id ? "Save new version" : "Save protocol"}</button>
          </div>
          {error && <p className="err" role="alert" style={{ marginTop: 10 }}>{error}</p>}
          {!name.trim() && <p className="approve-disabled" style={{ marginTop: 6 }}>· enter your name above to save or send</p>}
        </section>

        <section className="card hero">
          <div className="between">
            <span className="label">Live check against the bench</span>
            <span className="chip">{check ? `${check.moves} moves · about ${minutes > 0 ? `${minutes}m ` : ""}${seconds}s` : "…"}</span>
          </div>
          {check && (check.errors.length === 0
            ? <div className="row" style={{ marginTop: 10, gap: 10 }}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--jade)" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M5 12l5 5 10-11" /></svg>
                <span>Every rule passes: rack order, tube volumes, pour limits, well capacity, control well, sample first.</span>
              </div>
            : <div className="notice bad" style={{ marginTop: 10 }}><ul>{check.errors.map((e) => <li key={e}>{e}</li>)}</ul></div>)}
          <div className="between" style={{ marginTop: 14 }}>
            <p className="cap">Sending creates a run that still needs a named approval on the Run tab. The checker runs again at approval time.</p>
            <button type="button" className="btn primary reagent" disabled={!name.trim() || !check || check.errors.length > 0}
              onClick={() => act(async () => { const pid = await persist(); await toRun({ id: pid, by: name }); onSent(); })}>Send to Run</button>
          </div>
        </section>
      </div>

      <aside className="builder-aside">
        <div className="card">
          <label className="field" htmlFor="psearch">
            <span className="label">Saved protocols</span>
            <input id="psearch" className="input" placeholder="Search by name" value={search} onChange={(e) => setSearch(e.target.value)} />
          </label>
          <div style={{ marginTop: 10 }}>
            {list?.length === 0 && <p className="muted" style={{ padding: "14px 0" }}>{search ? "No match." : "None saved yet."}</p>}
            {list?.map((p) => (
              <button key={p._id} type="button" className="list-btn" aria-current={p._id === id} onClick={() => setId(p._id)}>
                <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {p.name} <span className="muted">· v{p.version} · {p.createdBy}</span>
                </span>
                <span className="row" style={{ gap: 4 }}>{[...new Set(p.rows.map((r) => r.tube))].map((t) => <span key={t} className="dot" style={{ background: DYE[t] }} />)}</span>
              </button>
            ))}
          </div>
        </div>
        <div className="card">
          <span className="label" style={{ marginBottom: 10, display: "block" }}>Tubes on the rack</span>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
            {(["A", "B", "C", "D"] as Tube[]).map((t) => (
              <li key={t} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <span className="dot" style={{ background: DYE[t], width: 12, height: 12 }} />
                <b>{t}</b>
                <span className="muted">· {STANDS[t]}</span>
              </li>
            ))}
          </ul>
        </div>
      </aside>
    </div>
  );
}
