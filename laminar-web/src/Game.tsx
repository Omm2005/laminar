import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../convex/_generated/api";
import { fmtSigned } from "./ui";

type Round = { target: number; onset: number; maxFlow: number };
const newRound = (): Round => ({
  target: Math.round((1 + Math.random() * 3) * 10) / 10,
  onset: 22 + Math.random() * 14,
  maxFlow: 1.35 + Math.random() * 0.3,
});
const MAX_TILT = 80, TILT_RATE = 55, FLIGHT_S = 0.3;

/** Hold to tilt, release to tip back. Liquid in the air keeps landing for 0.3 s. */
export default function Game({ name }: { name: string }) {
  const board = useQuery(api.qc.leaderboard);
  const save = useMutation(api.qc.submitScore);
  const [round, setRound] = useState<Round>(newRound);
  const [view, setView] = useState({ tilt: 0, ml: 0, air: 0 });
  const [result, setResult] = useState<{ ml: number; err: number } | null>(null);
  const [saveErr, setSaveErr] = useState("");
  const st = useRef({ tilt: 0, ml: 0, flight: [] as { t: number; v: number }[], holding: false, started: false, last: 0, settledAt: 0 });
  const raf = useRef(0);

  const loop = useCallback((now: number) => {
    const s = st.current;
    const dt = Math.min(0.05, (now - s.last) / 1000);
    s.last = now;
    s.tilt = Math.max(0, Math.min(MAX_TILT, s.tilt + (s.holding ? TILT_RATE : -TILT_RATE * 1.6) * dt));
    const over = Math.max(0, s.tilt - round.onset) / (MAX_TILT - round.onset);
    const flow = round.maxFlow * Math.pow(over, 1.3);
    if (flow > 0) s.flight.push({ t: now / 1000 + FLIGHT_S, v: flow * dt });
    let landed = 0;
    s.flight = s.flight.filter((p) => (p.t <= now / 1000 ? ((landed += p.v), false) : true));
    s.ml += landed;
    const air = s.flight.reduce((a, p) => a + p.v, 0);
    setView({ tilt: s.tilt, ml: s.ml, air });
    if (s.started && !s.holding && s.tilt === 0 && air === 0) {
      if (!s.settledAt) s.settledAt = now;
      if (now - s.settledAt > 150) {
        const ml = Math.round(s.ml * 100) / 100;
        const err = Math.round((ml - round.target) * 100) / 100;
        setResult({ ml, err });
        if (name.trim()) save({ player: name, targetMl: round.target, pouredMl: ml }).catch((e) => setSaveErr(String(e.message ?? e)));
        else setSaveErr("Enter your name at the top to save scores to the shared leaderboard.");
        return;
      }
    } else s.settledAt = 0;
    raf.current = requestAnimationFrame(loop);
  }, [round, name, save]);

  const press = (down: boolean) => {
    if (result) return;
    const s = st.current;
    s.holding = down;
    if (down && !s.started) {
      s.started = true;
      s.last = performance.now();
      raf.current = requestAnimationFrame(loop);
    }
  };
  const pressRef = useRef(press);
  pressRef.current = press;
  useEffect(() => {
    const typing = (e: KeyboardEvent) => ["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement).tagName);
    const kd = (e: KeyboardEvent) => { if (e.code === "Space" && !e.repeat && !typing(e)) { e.preventDefault(); pressRef.current(true); } };
    const ku = (e: KeyboardEvent) => { if (e.code === "Space" && !typing(e)) pressRef.current(false); };
    window.addEventListener("keydown", kd); window.addEventListener("keyup", ku);
    return () => { window.removeEventListener("keydown", kd); window.removeEventListener("keyup", ku); };
  }, []);
  useEffect(() => () => cancelAnimationFrame(raf.current), []);
  const again = () => {
    cancelAnimationFrame(raf.current);
    st.current = { tilt: 0, ml: 0, flight: [], holding: false, started: false, last: 0, settledAt: 0 };
    setView({ tilt: 0, ml: 0, air: 0 }); setResult(null); setSaveErr(""); setRound(newRound());
  };

  // Vial SVG (col a): the tilting container only.
  const VW = 180, VH = 220;
  // Beaker SVG (col b): the receiving vessel with the 0..5 scale.
  const BW = 220, BH = 240, wellTop = 24, wellH = 170, cap = 5;
  const yMl = (ml: number) => wellTop + wellH - (Math.min(ml, cap) / cap) * wellH;
  const r = board?.robot;

  return (
    <div className="game-grid">
      <section className="card hero game-main">
        <div className="game-head">
          <div>
            <span className="label">Beat the robot</span>
            <h3 className="game-title" style={{ marginTop: 6 }}>Pour exactly <em>{round.target.toFixed(1)} mL</em></h3>
          </div>
          <span className="chip">flow starts at a hidden angle</span>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 1, background: "var(--rule-2)" }}>
          {/* column (a): readouts + tilting vial */}
          <div style={{ background: "var(--panel)", padding: "12px 14px" }}>
            <div className="readout-grid" style={{ marginBottom: 10 }}>
              <div>
                <span className="label">Tilt</span>
                <div className="fig">{view.tilt.toFixed(0)}°</div>
              </div>
              <div>
                <span className="label">Poured</span>
                <div className="fig reagent">{view.ml.toFixed(2)} mL</div>
              </div>
            </div>
            <svg viewBox={`0 0 ${VW} ${VH}`} role="img" aria-label={`Tilt ${view.tilt.toFixed(0)} degrees`}
              style={{ width: "100%", height: "auto", touchAction: "none", userSelect: "none" }}
              onPointerDown={(e) => { (e.target as Element).setPointerCapture?.(e.pointerId); press(true); }}
              onPointerUp={() => press(false)} onPointerCancel={() => press(false)}>
              <g transform={`translate(${VW / 2} 70) rotate(${view.tilt})`} style={{ transformOrigin: "0 0" }}>
                <rect x="-18" y="-60" width="36" height="110" rx="6" fill="rgba(0,0,0,0)" stroke="var(--ink)" strokeWidth="2.4" />
                <rect x="-15" y="-10" width="30" height="56" rx="4" fill="var(--liq-a)" opacity="0.85" />
              </g>
              {view.air > 0.0005 && (
                <line x1={VW / 2 + 46} y1="96" x2={VW - 18} y2={VH - 24}
                  stroke="var(--liq-a)" strokeWidth={Math.min(5, 1 + view.air * 40)}
                  strokeLinecap="round" strokeDasharray="4 3" opacity="0.85" />
              )}
            </svg>
          </div>

          {/* column (b): beaker */}
          <div style={{ background: "var(--panel)", padding: "12px 14px" }}>
            <svg viewBox={`0 0 ${BW} ${BH}`} role="img" aria-label={`${view.ml.toFixed(2)} mL in beaker, target ${round.target.toFixed(1)}`}
              style={{ width: "100%", height: "auto", touchAction: "none" }}>
              <path d={`M60 ${wellTop} v${wellH - 14} q0 14 14 14 h72 q14 0 14 -14 v${-(wellH - 14)}`}
                fill="rgba(0,0,0,0)" stroke="var(--ink)" strokeWidth="2.4" />
              <rect x="62" y={yMl(view.ml)} width="96" height={wellTop + wellH - yMl(view.ml) - 1} rx="10" fill="var(--liq-a)" opacity="0.85" />
              <line x1="48" x2="170" y1={yMl(round.target)} y2={yMl(round.target)} stroke="var(--jade)" strokeDasharray="5 3" strokeWidth="1.5" />
              <text x="174" y={yMl(round.target) + 4} fontSize="11" fill="var(--jade)" fontFamily="var(--f-mono)">target</text>
              {[0, 1, 2, 3, 4, 5].map((v) => (
                <text key={v} x="46" y={yMl(v) + 4} textAnchor="end" fontSize="10" fill="var(--ink-3)" fontFamily="var(--f-mono)">{v}</text>
              ))}
            </svg>
            {!st.current.started && (
              <p className="mono" style={{ fontSize: 11, color: "var(--ink-3)", letterSpacing: ".08em", textAlign: "center", marginTop: 6 }}>
                Hold here or hold Space to tilt. Let go early.
              </p>
            )}
          </div>
        </div>

        {result ? (
          <div className="notice ok" style={{ marginTop: 14 }}>
            <p><b>{result.ml.toFixed(2)} mL</b> for a {round.target.toFixed(1)} mL target: {fmtSigned(result.err, 2)} mL.</p>
            <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>The arm doesn't guess either. Its camera reads the level 20 times a second, eases off past 80% of the target, and tips back early for the liquid still in the air, which is the 0.3 s you just felt.</p>
            {saveErr && <p className="err" style={{ marginTop: 6 }}>{saveErr}</p>}
            <div style={{ marginTop: 10 }}><button type="button" className="btn primary reagent" onClick={again}>Next round</button></div>
          </div>
        ) : (
          <button type="button" className="btn primary hold-tilt"
            onPointerDown={() => press(true)} onPointerUp={() => press(false)} onPointerLeave={() => press(false)}>
            Hold to tilt
          </button>
        )}
      </section>

      <aside className="game-aside">
        <div className="card">
          <span className="label" style={{ marginBottom: 10, display: "block" }}>Leaderboard · best round per person</span>
          <div className="tbl">
            <table>
              <thead><tr><th scope="col">Who</th><th scope="col" className="num">Target</th><th scope="col" className="num">Poured</th><th scope="col" className="num">Error</th><th scope="col" className="num">Rounds</th></tr></thead>
              <tbody>
                <tr className="laminar-arm">
                  <td><b>Laminar arm</b> <span className="tag reagent" style={{ marginLeft: 6 }}>real bench</span></td>
                  <td colSpan={2} className="muted">{r?.benchN ? `${r.benchN} real pours` : "waiting for real bench pours"}</td>
                  <td className="num">{r?.benchMae != null ? `±${r.benchMae.toFixed(2)}` : "—"}</td>
                  <td className="num">{r?.benchN ?? 0}</td>
                </tr>
                <tr>
                  <td>Simulated controller <span className="tag" style={{ marginLeft: 6 }}>not scored</span></td>
                  <td colSpan={2} className="muted">{r?.simN ?? 0} sim pours, mean abs error</td>
                  <td className="num">{r?.simMae != null ? `±${r.simMae.toFixed(2)}` : "—"}</td>
                  <td className="num">{r?.simN ?? 0}</td>
                </tr>
                {board?.humans.map((h) => (
                  <tr key={h._id}>
                    <td>{h.player}</td>
                    <td className="num">{h.targetMl.toFixed(1)}</td>
                    <td className="num">{h.pouredMl.toFixed(2)}</td>
                    <td className="num">{fmtSigned(h.errorMl, 2)}</td>
                    <td className="num">{h.rounds}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {board?.humans.length === 0 && <p className="muted" style={{ marginTop: 10 }}>No human scores yet. Be the first.</p>}
        </div>
        <p className="cap">The arm's row averages only real bench pours. Simulated pours are shown separately and never count as the robot's score. The game never moves the arm.</p>
      </aside>
    </div>
  );
}
