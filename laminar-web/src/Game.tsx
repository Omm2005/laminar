import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../convex/_generated/api";
import { fmtSigned } from "./ui";

/* ───────────────── physics constants ───────────────── */
const VIAL_FULL = 5;              // mL, generous so a 1-4 mL target is always reachable
const OMEGA = 5;                  // critically damped spring on tilt
const SUBSTEP_MAX = 0.45 / OMEGA; // ω·h < 0.45
const HELD_TARGET = 72;
const Q_PEAK = 1.6, Q_REF_OVER = 20; // q = 1.6 mL/s at θ0+20° with full vial
const K = Q_PEAK / Math.pow(Q_REF_OVER, 1.5); // closed-form k
const Q_CAP = 3.0;
const FLIGHT_TAU = 0.28;          // s, time of flight from lip to beaker
const TRICKLE_TAU = 0.18;         // s, flow decay once θ < θ0 after release
const END_HOLD = 0.7;             // s of quiescence before scoring

const newRound = () => ({
  target: Math.round((1 + Math.random() * 3) * 10) / 10, // 1.0..4.0 mL
  theta0: 26 + Math.random() * 18,                        // hidden onset
});

type Round = ReturnType<typeof newRound>;

type S = {
  theta: number;
  v: number;
  q: number;
  remaining: number;
  received: number;
  flight: { landAt: number; v: number }[];
  wobble: number;      // 0..1 envelope
  held: boolean;
  started: boolean;
  settledAt: number;   // ms the quiescence window began, 0 if active
  last: number;        // ms of previous frame
  wasAbove: boolean;   // whether θ was above θ0 recently
};

const freshS = (): S => ({
  theta: 0, v: 0, q: 0,
  remaining: VIAL_FULL, received: 0,
  flight: [], wobble: 0,
  held: false, started: false, settledAt: 0, last: 0, wasAbove: false,
});

export default function Game({ name }: { name: string }) {
  const board = useQuery(api.qc.leaderboard);
  const save = useMutation(api.qc.submitScore);
  const [round, setRound] = useState<Round>(newRound);
  const [view, setView] = useState({ theta: 0, poured: 0, surfaceWobble: 0, q: 0, remaining: VIAL_FULL });
  const [result, setResult] = useState<{ ml: number; err: number } | null>(null);
  const [saveErr, setSaveErr] = useState("");
  const st = useRef<S>(freshS());
  const raf = useRef(0);
  const paused = useRef(false);

  const loop = useCallback((now: number) => {
    const s = st.current;
    if (paused.current || document.hidden) { s.last = now; raf.current = requestAnimationFrame(loop); return; }
    const rawDt = s.last ? Math.min(0.05, (now - s.last) / 1000) : 0;
    s.last = now;

    // ─── spring on tilt ───
    const target = s.held ? HELD_TARGET : 0;
    const substeps = Math.max(1, Math.ceil(rawDt / SUBSTEP_MAX));
    const h = rawDt / substeps;
    for (let k = 0; k < substeps; k++) {
      s.v += (OMEGA * OMEGA * (target - s.theta) - 2 * OMEGA * s.v) * h;
      s.theta += s.v * h;
    }

    // ─── flow ───
    const above = s.theta >= round.theta0;
    let q = 0;
    if (above && s.remaining > 1e-5) {
      const over = s.theta - round.theta0;
      q = Math.min(Q_CAP, K * Math.pow(over, 1.5) * Math.sqrt(Math.max(0, s.remaining) / VIAL_FULL));
      s.wasAbove = true;
    } else if (s.wasAbove && s.q > 1e-5 && rawDt > 0) {
      q = s.q * Math.exp(-rawDt / TRICKLE_TAU);
      if (q < 1e-5) { q = 0; s.wasAbove = false; }
    }
    s.q = q;

    // ─── emit + flight ───
    if (q > 0 && rawDt > 0) {
      const emitted = Math.min(s.remaining, q * rawDt);
      s.remaining -= emitted;
      s.flight.push({ landAt: now / 1000 + FLIGHT_TAU, v: emitted });
    }
    const nowS = now / 1000;
    let landed = 0;
    s.flight = s.flight.filter((p) => (p.landAt <= nowS ? (landed += p.v, false) : true));
    s.received += landed;

    // ─── wobble envelope: rise while pouring, decay after ───
    const targetWobble = q > 0 ? 1 : 0;
    const wTau = q > 0 ? 0.2 : 0.5;
    s.wobble += (targetWobble - s.wobble) * (1 - Math.exp(-rawDt / wTau));
    const surfaceWobble = s.wobble * 2 * Math.sin(2 * Math.PI * 3.2 * nowS);

    // ─── end-of-round latch ───
    if (s.started && !s.held && s.q < 0.005 && s.theta < 2 && s.flight.length === 0) {
      if (!s.settledAt) s.settledAt = now;
      if (now - s.settledAt > END_HOLD * 1000) {
        const ml = Math.round(s.received * 100) / 100;
        const err = Math.round((ml - round.target) * 100) / 100;
        setResult({ ml, err });
        if (name.trim()) save({ player: name, targetMl: round.target, pouredMl: ml }).catch((e) => setSaveErr(String(e.message ?? e)));
        else setSaveErr("Enter your name at the top to save scores to the shared leaderboard.");
        return;
      }
    } else {
      s.settledAt = 0;
    }

    setView({ theta: s.theta, poured: s.received, surfaceWobble, q, remaining: s.remaining });
    raf.current = requestAnimationFrame(loop);
  }, [round, name, save]);

  const press = useCallback((down: boolean) => {
    if (result) return;
    const s = st.current;
    s.held = down;
    if (down && !s.started) {
      s.started = true;
      s.last = 0;
      raf.current = requestAnimationFrame(loop);
    }
  }, [result, loop]);

  const pressRef = useRef(press);
  pressRef.current = press;
  useEffect(() => {
    const typing = (e: KeyboardEvent) => ["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement).tagName);
    const kd = (e: KeyboardEvent) => { if (e.code === "Space" && !e.repeat && !typing(e)) { e.preventDefault(); pressRef.current(true); } };
    const ku = (e: KeyboardEvent) => { if (e.code === "Space" && !typing(e)) pressRef.current(false); };
    const vis = () => { paused.current = document.hidden; };
    window.addEventListener("keydown", kd);
    window.addEventListener("keyup", ku);
    document.addEventListener("visibilitychange", vis);
    return () => { window.removeEventListener("keydown", kd); window.removeEventListener("keyup", ku); document.removeEventListener("visibilitychange", vis); };
  }, []);
  useEffect(() => () => cancelAnimationFrame(raf.current), []);

  const again = () => {
    cancelAnimationFrame(raf.current);
    st.current = freshS();
    setView({ theta: 0, poured: 0, surfaceWobble: 0, q: 0, remaining: VIAL_FULL });
    setResult(null); setSaveErr(""); setRound(newRound());
  };

  const r = board?.robot;

  /* ───────────────── drawing ───────────────── */
  // One SVG for both figures so the stream can connect vial lip → beaker surface.
  const W = 460, H = 260;
  const VIAL_W = 42, VIAL_H = 140;
  const lipX = 150, lipY = 44;              // the pour point; the vial hangs below and left of here
  const beakerX = 260, beakerW = 150;       // beaker outer
  const beakerTop = 32, beakerH = 200;      // beaker interior
  const SCALE_MAX = 5;
  const beakerFillH = (view.poured / SCALE_MAX) * beakerH;
  const beakerSurfaceY = beakerTop + beakerH - beakerFillH + view.surfaceWobble;
  // Vial local: anchor at (0,0) = lip (top-right); body extends from x=-VIAL_W..0, y=0..VIAL_H
  const surfaceLocal = (1 - view.remaining / VIAL_FULL) * VIAL_H;

  // Stream path: from the lip (world coords, after rotation lip stays fixed) to beaker surface.
  const streamVisible = view.q > 0.02;
  const streamWidth = 1.5 + 4 * Math.sqrt(Math.min(view.q, Q_CAP) / Q_CAP);
  const streamPath = `M ${lipX} ${lipY + 2} C ${lipX + 60} ${lipY + 40}, ${beakerX + beakerW / 2 - 40} ${beakerSurfaceY - 60}, ${beakerX + beakerW / 2} ${beakerSurfaceY - 2}`;
  // Animate the stream dashoffset proportional to q.
  const streamDur = view.q > 0.02 ? Math.max(0.5, 1.4 / view.q) : 2;

  const started = st.current.started;

  return (
    <div className="game-grid">
      <section className="card hero game-main">
        <div className="game-head">
          <div>
            <span className="t-label">Beat the robot</span>
            <h3 className="game-title" style={{ marginTop: 6 }}>Pour exactly <em>{round.target.toFixed(1)} mL</em></h3>
          </div>
          <span className="chip">flow starts at a hidden angle</span>
        </div>

        <div className="readout-grid" style={{ marginTop: 12 }}>
          <div>
            <span className="t-label">Tilt</span>
            <div className="t-num">{view.theta.toFixed(0)}°</div>
          </div>
          <div>
            <span className="t-label">Poured</span>
            <div className="t-num" style={{ color: "var(--reagent)" }}>{view.poured.toFixed(2)} mL</div>
          </div>
        </div>

        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Tilt ${view.theta.toFixed(0)} degrees, ${view.poured.toFixed(2)} mL in beaker`}
          style={{ width: "100%", height: "auto", marginTop: 10, touchAction: "none", userSelect: "none" }}
          onPointerDown={(e) => { (e.target as Element).setPointerCapture?.(e.pointerId); press(true); }}
          onPointerUp={() => press(false)} onPointerCancel={() => press(false)}>
          <defs>
            <clipPath id="vial-shape">
              <rect x={-VIAL_W} y={0} width={VIAL_W} height={VIAL_H} rx="6" />
            </clipPath>
          </defs>

          {/* vial — rotate about the lip */}
          <g transform={`translate(${lipX} ${lipY}) rotate(${view.theta})`}>
            {/* liquid: in a group that counter-rotates about the same point, clipped to the vial */}
            <g clipPath="url(#vial-shape)">
              <g transform={`rotate(${-view.theta})`}>
                <rect x={-VIAL_W * 4} y={surfaceLocal} width={VIAL_W * 8} height={VIAL_H * 4}
                  fill="var(--liq-a)" opacity="0.85" />
              </g>
            </g>
            <rect x={-VIAL_W} y={0} width={VIAL_W} height={VIAL_H} rx="6"
              fill="none" stroke="var(--ink)" strokeWidth="2.4" />
            {/* mouth highlight */}
            <line x1={-VIAL_W + 2} y1={1.5} x2={-1.5} y2={1.5} stroke="var(--ink)" strokeWidth="2.4" strokeLinecap="round" />
          </g>

          {/* beaker — straight walls with a stepped base */}
          <g transform={`translate(${beakerX} ${beakerTop})`}>
            <path d={`M 0 0 v ${beakerH - 14} q 0 14 14 14 h ${beakerW - 28} q 14 0 14 -14 v ${-(beakerH - 14)}`}
              fill="none" stroke="var(--ink)" strokeWidth="2.4" />
            {/* fill */}
            <rect x={2} y={beakerH - beakerFillH + view.surfaceWobble} width={beakerW - 4}
              height={Math.max(0, beakerFillH - view.surfaceWobble)} rx={10} fill="var(--liq-a)" opacity="0.85" />
            {/* 0..5 ticks on the left */}
            {Array.from({ length: SCALE_MAX + 1 }, (_, v) => {
              const yT = beakerH - (v / SCALE_MAX) * beakerH;
              return (
                <g key={v}>
                  <line x1={-4} x2={2} y1={yT} y2={yT} stroke="var(--ink-3)" strokeWidth="1" />
                  <text x={-8} y={yT + 4} textAnchor="end" fontSize="10" fill="var(--ink-3)" fontFamily="var(--mono)">{v}</text>
                </g>
              );
            })}
            {/* target line (jade, dashed) */}
            <g>
              <line x1={-6} x2={beakerW + 6} y1={beakerH - (round.target / SCALE_MAX) * beakerH}
                y2={beakerH - (round.target / SCALE_MAX) * beakerH}
                stroke="var(--jade)" strokeDasharray="5 3" strokeWidth="1.5" />
              <text x={beakerW + 10} y={beakerH - (round.target / SCALE_MAX) * beakerH + 4}
                fontSize="10" fill="var(--jade)" fontFamily="var(--mono)">target</text>
            </g>
          </g>

          {/* stream — rendered last so it sits on top */}
          {streamVisible && (
            <path d={streamPath} fill="none" stroke="var(--liq-a)" strokeWidth={streamWidth}
              strokeDasharray="9 7" strokeLinecap="round" opacity="0.9">
              <animate attributeName="stroke-dashoffset" from="0" to="-16"
                dur={`${streamDur.toFixed(2)}s`} repeatCount="indefinite" />
            </path>
          )}
        </svg>

        {!started && (
          <p className="t-label" style={{ textAlign: "center", marginTop: 6, color: "var(--ink-3)" }}>
            Hold here or hold Space to tilt. Let go early.
          </p>
        )}

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
          <span className="t-label" style={{ marginBottom: 10, display: "block" }}>Leaderboard · best round per person</span>
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
