import { useEffect, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";

export default function Settings() {
  const flow = useQuery(api.flowTable.get, {});
  const update = useMutation(api.flowTable.update);

  const [onset, setOnset] = useState("");
  const [pour, setPour] = useState("");
  const [rates, setRates] = useState<{ deg: string; mlPerS: string }[]>([]);

  useEffect(() => {
    if (!flow) return;
    setOnset(String(flow.onsetDeg));
    setPour(String(flow.pourDeg));
    setRates(flow.rates.map((r) => ({ deg: String(r.deg), mlPerS: String(r.mlPerS) })));
  }, [flow?._id, flow?.updatedAt]);

  async function save() {
    const parsedRates = rates
      .map((r) => ({ deg: Number(r.deg), mlPerS: Number(r.mlPerS) }))
      .filter((r) => Number.isFinite(r.deg) && Number.isFinite(r.mlPerS));
    await update({
      onsetDeg: Number(onset),
      pourDeg: Number(pour),
      rates: parsedRates,
      updatedBy: "web",
    });
  }

  if (!flow) {
    return (
      <div className="card">
        <h2>Flow table</h2>
        <p className="mono">no flow table yet — run <code>node scripts/seed.mjs</code> to seed from pour/data/pour.yaml</p>
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Flow table</h2>
      <p className="mono">
        last updated {new Date(flow.updatedAt).toLocaleString()} by {flow.updatedBy ?? "?"}
      </p>
      <div className="row" style={{ marginTop: 8 }}>
        <label style={{ flex: "1 1 180px" }}>
          <div className="mono">onsetDeg</div>
          <input value={onset} onChange={(e) => setOnset(e.target.value)} />
        </label>
        <label style={{ flex: "1 1 180px" }}>
          <div className="mono">pourDeg (default)</div>
          <input value={pour} onChange={(e) => setPour(e.target.value)} />
        </label>
      </div>
      <h3 style={{ marginTop: 14 }}>rates</h3>
      <div className="rate-table">
        <div className="mono">angle (°)</div>
        <div className="mono">mL / s</div>
        <div></div>
        {rates.map((r, i) => (
          <RateRow
            key={i}
            r={r}
            onChange={(nr) => setRates((prev) => prev.map((x, idx) => idx === i ? nr : x))}
            onDelete={() => setRates((prev) => prev.filter((_, idx) => idx !== i))}
          />
        ))}
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <button className="ghost" onClick={() => setRates((p) => [...p, { deg: "", mlPerS: "" }])}>+ row</button>
        <button className="primary" onClick={save}>Save</button>
      </div>
    </div>
  );
}

function RateRow({
  r, onChange, onDelete,
}: {
  r: { deg: string; mlPerS: string };
  onChange: (nr: { deg: string; mlPerS: string }) => void;
  onDelete: () => void;
}) {
  return (
    <>
      <input value={r.deg} onChange={(e) => onChange({ ...r, deg: e.target.value })} />
      <input value={r.mlPerS} onChange={(e) => onChange({ ...r, mlPerS: e.target.value })} />
      <button className="ghost" onClick={onDelete}>×</button>
    </>
  );
}
