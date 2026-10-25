import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../convex/_generated/api";
import { fmtSigned } from "./ui";

type Round = { target: number; onset: number; maxFlow: number };
const newRound = (): Round => ({
  target: Math.round((1 + Math.random() * 3) * 10) / 10, // 1.0–4.0 mL
  onset: 22 + Math.random() * 14,                       // no flow below this tilt
  maxFlow: 1.35 + Math.random() * 0.3,                  // ~1.5 mL/s at full tilt
});
const MAX_TILT = 80, TILT_RATE = 55, FLIGHT_S = 0.3;

/** Hold to tilt, release to tip back. Liquid already in the air keeps landing for 0.3 s. */
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
  // Keyboard handlers read the latest press() through a ref, so they are attached once and never
  // re-run on the per-frame re-renders (which used to cancel the animation loop after one frame).
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

  const W = 420, H = 300, wellTop = 150, wellH = 120, cap = 5;
  const yMl = (ml: number) => wellTop + wellH - (Math.min(ml, cap) / cap) * wellH;
  const r = board?.robot;
  return (
    <div className="run-grid">
      <div className="panel stack" style={{ gap: 12 }}>
        <div className="between">
          <div>
            <p className="label">Beat the robot</p>
            <h3 style={{ fontSize: "var(--s-3)", marginTop: 4 }}>Pour exactly <span style={{ color: "var(--dye-a)" }}>{round.target.toFixed(1)} mL</span></h3>
          </div>
          <span className="chip">flow starts at a hidden angle</span>
        </div>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Tilt ${view.tilt.toFixed(0)} degrees, ${view.ml.toFixed(2)} mL poured`} style={{ width: "100%", height: "auto", touchAction: "none", userSelect: "none" }}
          onPointerDown={(e) => { (e.target as Element).setPointerCapture?.(e.pointerId); press(true); }} onPointerUp={() => press(false)} onPointerCancel={() => press(false)}>
          <g transform={`translate(150 70) rotate(${view.tilt})`} style={{ transformOrigin: "0 0" }}>
            <rect x="-14" y="-60" width="28" height="80" rx="6" fill="var(--glass)" stroke="var(--ink)" strokeWidth="1.5" />
            <rect x="-12" y="-20" width="24" height="38" rx="4" fill="var(--dye-a)" opacity="0.85" />
          </g>
          {view.air > 0.0005 && <line x1="196" y1="96" x2="214" y2={yMl(view.ml)} stroke="var(--dye-a)" strokeWidth={Math.min(5, 1 + view.air * 40)} strokeLinecap="round" opacity="0.8" />}
          <path d={`M180 ${wellTop} v${wellH - 12} q0 12 12 12 h56 q12 0 12 -12 v${-(wellH - 12)}`} fill="var(--glass)" stroke="var(--ink)" strokeWidth="1.5" />
          <rect x="182" y={yMl(view.ml)} width="76" height={wellTop + wellH - yMl(view.ml) - 1} rx="9" fill="var(--dye-a)" opacity="0.85" />
          <line x1="172" x2="268" y1={yMl(round.target)} y2={yMl(round.target)} stroke="var(--ok)" strokeDasharray="5 3" strokeWidth="1.5" />
          <text x="274" y={yMl(round.target) + 4} fontSize="11" fill="var(--ok)">target</text>
          {[0, 1, 2, 3, 4, 5].map((v) => <text key={v} x="168" y={yMl(v) + 4} textAnchor="end" fontSize="10" fill="var(--muted)">{v}</text>)}
          <text x="16" y="24" fontSize="12" fill="var(--muted)">tilt {view.tilt.toFixed(0)}°</text>
          <text x="16" y="42" fontSize="12" fill="var(--ink)">poured {view.ml.toFixed(2)} mL</text>
          {!st.current.started && <text x={W / 2} y={H - 8} textAnchor="middle" fontSize="12" fill="var(--muted)">Hold here or hold Space to tilt. Let go early.</text>}
        </svg>
        {result ? (
          <div className="notice ok stack" style={{ gap: 6 }}>
            <p><b>{result.ml.toFixed(2)} mL</b> for a {round.target.toFixed(1)} mL target: {fmtSigned(result.err, 2)} mL.</p>
            <p className="muted" style={{ fontSize: "0.875rem" }}>The arm doesn’t guess either. Its camera reads the level 20 times a second, eases off past 80% of the target, and tips back early for the liquid still in the air, which is the 0.3 s you just felt.</p>
            {saveErr && <p className="err">{saveErr}</p>}
            <div><button className="btn primary" onClick={again}>Next round</button></div>
          </div>
        ) : (
          <button className="btn" onPointerDown={() => press(true)} onPointerUp={() => press(false)} onPointerLeave={() => press(false)}>Hold to tilt</button>
        )}
      </div>
      <div className="panel">
        <p className="label">Leaderboard · best round per person</p>
        <div className="tbl"><table>
          <thead><tr><th>Who</th><th className="num">Target</th><th className="num">Poured</th><th className="num">Error</th><th className="num">Rounds</th></tr></thead>
          <tbody>
            <tr style={{ background: "var(--glass)" }}>
              <td><b>Laminar arm</b></td><td colSpan={2} className="muted">{r?.benchN ? `${r.benchN} real pours` : "waiting for real bench pours"}</td>
              <td className="num">{r?.benchMae != null ? `±${r.benchMae.toFixed(2)}` : "—"}</td><td className="num">{r?.benchN ?? 0}</td>
            </tr>
            <tr style={{ background: "var(--glass)" }}>
              <td>Simulated controller</td><td colSpan={2} className="muted">{r?.simN ?? 0} sim pours, mean abs error</td>
              <td className="num">{r?.simMae != null ? `±${r.simMae.toFixed(2)}` : "—"}</td><td className="num">{r?.simN ?? 0}</td>
            </tr>
            {board?.humans.map((h) => (
              <tr key={h._id}><td>{h.player}</td><td className="num">{h.targetMl.toFixed(1)}</td><td className="num">{h.pouredMl.toFixed(2)}</td><td className="num">{fmtSigned(h.errorMl, 2)}</td><td className="num">{h.rounds}</td></tr>
            ))}
          </tbody>
        </table></div>
        {board?.humans.length === 0 && <p className="muted" style={{ marginTop: 8 }}>No human scores yet. Be the first.</p>}
        <p className="cap">The arm’s row averages only real bench pours. Simulated pours are shown separately and never count as the robot’s score. The game never moves the arm.</p>
      </div>
    </div>
  );
}
