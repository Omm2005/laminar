import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useAction, useQuery } from "convex/react";
import { api } from "../convex/_generated/api";
import { Section, ago, tint, useReveal, useScrollTheme, useSectionPause } from "./ui";
import LiveArm from "./robot/LiveArm";
import Arm3D, { type Telem } from "./robot/Arm3D";

// ---------------------------------------------------------------- the adaptive pour (illustrative)

const TARGET = 2.0, BAND = 0.2, LEAD_OUT = 0.35, T_END = 6;

/** Closed loop: the station camera reads the column at 5 Hz; the wrist rolls back 0.35 mL early, and the
 *  liquid still in flight lands on target. */
function closedLoop(t: number) {
  const onset = 1.1, rate = 0.95;
  const stopAt = onset + (TARGET - LEAD_OUT) / rate;
  if (t < onset) return 0;
  if (t < stopAt) return (t - onset) * rate;
  return TARGET - 0.02 - (LEAD_OUT - 0.02) * Math.exp(-(t - stopAt) / 0.35);
}
/** Fixed angle: the identical wrist motion with the camera ignored. Onset and flow change with fill level
 *  and how the lip breaks, so the same two-second tilt gives a different volume each time. */
const FIXED = [
  { onset: 1.3, rate: 0.78 },
  { onset: 1.0, rate: 0.95 },
  { onset: 0.85, rate: 1.12 },
];
function fixedAngle(t: number, f: (typeof FIXED)[number]) {
  const end = 3.3;
  if (t < f.onset) return 0;
  const v = (Math.min(t, end) - f.onset) * f.rate;
  return t <= end ? v : v + 0.12 * (1 - Math.exp(-(t - end) / 0.3));
}

function PourChart() {
  const [t, setT] = useState(T_END);
  const raf = useRef(0);
  const replay = () => {
    cancelAnimationFrame(raf.current);
    const start = performance.now();
    const step = (now: number) => {
      const s = Math.min(T_END, ((now - start) / 1000) * 1.25);
      setT(s);
      if (s < T_END) raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
  };
  useEffect(() => () => cancelAnimationFrame(raf.current), []);
  const W = 520, H = 330, L = 34, R = 52, T = 22, B = 30;
  const x = (s: number) => L + (s / T_END) * (W - L - R);
  const y = (ml: number) => T + (1 - ml / 3) * (H - T - B);
  const path = (f: (s: number) => number) => Array.from({ length: 121 }, (_, i) => (i / 120) * t).map((s) => `${x(s).toFixed(1)},${y(f(s)).toFixed(1)}`).join(" ");
  const finals = FIXED.map((f) => fixedAngle(T_END, f));
  return (
    <figure className="panel" style={{ margin: 0 }}>
      <div className="between" style={{ marginBottom: 10, flexWrap: "nowrap" }}>
        <span className="label">Delivered volume · 2.00 mL into ch 04</span>
        <button className="btn small" onClick={replay}>Replay</button>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Closed-loop pour lands inside the plus or minus 0.2 mL band; three fixed-angle pours land at different volumes" style={{ width: "100%", height: "auto", display: "block" }}>
        <rect x={L} y={y(TARGET + BAND)} width={W - L - R} height={y(TARGET - BAND) - y(TARGET + BAND)} fill="var(--accent)" opacity="0.1" />
        <line x1={L} x2={W - R} y1={y(TARGET)} y2={y(TARGET)} stroke="var(--accent)" strokeDasharray="2 4" opacity="0.6" />
        <text x={L + 6} y={y(TARGET + BAND) - 7} fontSize="13" fill="var(--accent)">±0.2 mL band</text>
        {[0, 1, 2, 3].map((v) => <text key={v} x={L - 8} y={y(v) + 4} textAnchor="end" fontSize="13" fill="var(--muted)">{v}</text>)}
        {[0, 1, 2, 3, 4, 5, 6].map((s) => <text key={s} x={x(s)} y={H - 8} textAnchor="middle" fontSize="13" fill="var(--muted)">{s}{s === 6 ? " s" : ""}</text>)}
        {FIXED.map((f, i) => <polyline key={i} points={path((s) => fixedAngle(s, f))} fill="none" stroke="var(--muted)" strokeWidth="1.5" strokeDasharray="5 4" />)}
        <polyline points={path(closedLoop)} fill="none" stroke="var(--dye-a)" strokeWidth="2.6" />
        <circle cx={x(t)} cy={y(closedLoop(t))} r="5" fill="var(--dye-a)" />
        {t >= T_END && finals.map((v, i) => <text key={i} x={x(T_END) + 8} y={y(v) + 5} fontSize="13" fill="var(--muted)">{v.toFixed(2)}</text>)}
        {t >= T_END && <text x={x(T_END) + 8} y={y(closedLoop(T_END)) + 5} fontSize="13" fontWeight="600" fill="var(--dye-a)">{closedLoop(T_END).toFixed(2)}</text>}
      </svg>
      <div className="row" style={{ gap: 18, marginTop: 8, fontSize: "0.8125rem" }}>
        <span><span className="dot" style={{ background: "var(--dye-a)" }} /> Closed loop · {closedLoop(t).toFixed(2)} mL</span>
        <span className="muted">- - Fixed angle, three tries</span>
      </div>
      <figcaption className="cap">Illustrative shapes, not measured data. The shaded band is the ±0.2 mL the protocol allows. The dashed traces are the identical wrist motion with the camera ignored.</figcaption>
    </figure>
  );
}

// ---------------------------------------------------------------- hero arm

/** While the bench is idle, loop tonight's longest real run from Tiger Data (measured joints, 6×).
 *  When a run starts, show the live arm instead. */
function HeroArm() {
  const bench = useQuery(api.bench.state);
  const list = useAction(api.tiger.armRuns);
  const detail = useAction(api.tiger.armRunDetail);
  const [run, setRun] = useState<{ name: string; frames: [number, number[]][] } | null>(null);
  const [samples, setSamples] = useState<Telem[]>([]);
  useEffect(() => {
    let alive = true;
    (async () => {
      const r = await list({ limit: 12 });
      if (!("available" in r) || !r.available) return;
      const pick = [...r.runs].filter((x) => x.status === "done" && x.ended).sort((a, b) => (b.ended! - b.started) - (a.ended! - a.started))[0];
      if (!pick) return;
      const d = await detail({ runId: pick.runId });
      if (!alive || !("found" in d) || !d.found) return;
      const by = new Map<number, number[]>();
      for (const p of d.trace) { const row = by.get(p.s) ?? [0, 0, 0, 0, 0, 0]; row[p.j - 1] = p.pos; by.set(p.s, row); }
      setRun({ name: d.name ?? d.runId, frames: [...by.entries()].sort((a, b) => a[0] - b[0]) });
    })().catch(() => {});
    return () => { alive = false; };
  }, [list, detail]);
  const SPEED = 6;
  useEffect(() => {
    if (!run?.frames.length) return;
    let timer: ReturnType<typeof setTimeout>;
    const play = () => {
      const t0 = Date.now() + 200, s0 = run.frames[0][0];
      setSamples(run.frames.map(([s, joints]) => ({ t: t0 + ((s - s0) * 1000) / SPEED, joints })));
      timer = setTimeout(play, ((run.frames.at(-1)![0] - s0) * 1000) / SPEED + 1500);
    };
    play();
    return () => clearTimeout(timer);
  }, [run]);
  const label = useMemo(() => run?.name.split(" --")[0], [run]);
  if (bench?.running || !run) return <LiveArm height={420} />;
  return (
    <div style={{ height: 420, position: "relative", background: "#0f1618" }}>
      <Arm3D samples={samples} status="running" gripperDeg={30} cameraPreset="wide" />
      <span className="arm-tag mono" title={label}>real run replayed from Tiger Data · {SPEED}×</span>
    </div>
  );
}

// ---------------------------------------------------------------- live link

function LiveLink() {
  const samples = useQuery(api.robot.recent);
  const bench = useQuery(api.bench.state);
  const latest = samples?.at(-1);
  const measured = latest?.source === "bench";
  const j = latest?.joints;
  const rows: [string, string][] = [
    ["J1 base", j ? `${j[0].toFixed(1)}°` : "—"],
    ["J2 shoulder", j ? `${j[1].toFixed(1)}°` : "—"],
    ["J3 elbow", j ? `${j[2].toFixed(1)}°` : "—"],
    ["J6 wrist", j ? `${j[5].toFixed(1)}°` : "—"],
    ["Gripper", latest?.gripperDeg != null ? `${latest.gripperDeg.toFixed(0)}°` : "—"],
    ["Move", latest?.move ?? (bench?.running ? "starting" : "at rest")],
  ];
  return (
    <div className="grid2" style={{ alignItems: "start" }}>
      <div className="panel" style={{ padding: 0, overflow: "hidden" }}><LiveArm height={380} /></div>
      <div className="stack" style={{ gap: 16 }}>
        <div className="tiles">
          {rows.map(([k, v]) => <div key={k}><span className="label">{k}</span><b style={{ fontSize: "1.25rem" }} className="num">{v}</b></div>)}
        </div>
        <p className="notice">
          <span className={`chip ${measured ? "ok" : "live"}`} style={{ marginBottom: 6 }}>{measured ? "measured joints from the bench" : "simulated joints"}</span><br />
          {measured
            ? `The arm above mirrors the real one. Last sample ${latest ? ago(latest.t) : "—"}.`
            : "Until the bench laptop publishes, the arm above plays the latest run through the team's taught poses. When it streams joints, the same view switches to the real arm."}
        </p>
        <div className="row"><a className="btn primary" href="#console">Open the console</a></div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- page

const SPECS: [string, string][] = [["±0.2", "mL tolerance"], ["12", "channels"], ["5 Hz", "vision loop"], ["7", "driven axes"], ["15 mL", "conical, 17 mm"]];

const PAUSE_AT = ["method", "pour", "hardware", "why", "agent", "live", "team"];

export default function Site() {
  useReveal();
  useScrollTheme();
  useSectionPause(PAUSE_AT, 1000);
  return (
    <main>
      <section className="hero tinted in" style={{ "--accent": tint(0, 8) } as CSSProperties}>
        <div className="wrap hero-grid">
          <div>
            <p className="label">Autonomous liquid handling · Cal Hacks 13.0</p>
            <h1 style={{ marginTop: 14 }}>±0.2 mL. <em>Every time.</em></h1>
            <p className="lede">A seven-axis arm that keeps tilting until the camera says the channel holds exactly what the protocol asked for.</p>
            <div className="cta">
              <a className="btn primary" href="#method">See the method</a>
              <a className="btn" href="#console">Open the console</a>
            </div>
            <div className="specs">
              {SPECS.map(([b, s]) => <div key={s}><b>{b}</b><span>{s}</span></div>)}
            </div>
          </div>
          <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
            <HeroArm />
          </div>
        </div>
      </section>

      <Section id="method" accent={tint(1, 8)} label="Method" title="Four moves. One of them is hard."
        intro="Lifting and carrying a vial is solved. Knowing when to stop pouring is not. So everything up to the pour runs on fixed, measured poses, and the camera only has to do the one job that needs it.">
        <div className="moves4">
          <div><span className="label">01 / Acquire</span><h3>The finger bends, not the tube</h3><p>Fin-ray fingers close to an 11 mm gap. Press a tube into the flat face and the whole finger curls around it, so a stalled motor flexes plastic instead of crushing a 0.9 mm wall.</p></div>
          <div><span className="label">02 / Transport</span><h3>The wrist never moves</h3><p>Six joints carry the vial across the bench with the wrist roll locked at zero. The meniscus never breaks, so the volume measured at the rack is the volume that arrives.</p></div>
          <div><span className="label">03 / Align</span><h3>Look first, then commit</h3><p>A camera on the gripper, pitched 38° out over the fingertips, finds the marker on the station and corrects the approach before the arm descends. Move the station mid-demo; it still finds it.</p></div>
          <div className="hard"><span className="label">04 / Pour</span><h3>One joint, closed loop</h3><p>Only the wrist rolls. A second camera reads the rising column in the channel and feeds volume back five times a second, so the tilt stops on a measured volume, not a stopwatch.</p></div>
        </div>
      </Section>

      <Section id="pour" accent={tint(2, 8)} label="The adaptive pour" title="A tilt angle gets you a pour. It doesn’t get you a volume.">
        <div className="grid2" style={{ alignItems: "start" }}>
          <div className="prose">
            <p>Tip the same vial twice, to the same angle, for the same two seconds, and you get two different volumes. Surface tension, how full the tube started, how cleanly the lip broke: all of it moves the number.</p>
            <p>So the angle is not the setpoint. The volume is. The wrist rolls in, the station camera measures the column climbing in the channel, and the controller trims the tilt until the delivered volume reaches target, then rolls back out and holds.</p>
            <dl className="facts">
              <div><dt>Controlled joint</dt><dd>J6 wrist roll</dd></div>
              <div><dt>Feedback signal</dt><dd>column height → mL</dd></div>
              <div><dt>Loop rate</dt><dd>5 Hz</dd></div>
              <div><dt>Lead-out</dt><dd>0.35 mL anticipation</dd></div>
              <div><dt>Every other joint</dt><dd>fixed, from YAML</dd></div>
            </dl>
          </div>
          <PourChart />
        </div>
      </Section>

      <Section id="hardware" accent={tint(3, 8)} label="Hardware" title="Stock arm. Everything that touches glass, we printed.">
        <div className="hw">
          <div><span className="label">Arm</span><h3>reBot B601-RS</h3><p>Seven RobStride joints on a 1 Mbps CAN bus, driven straight from a laptop through a USB-CAN adapter. No vendor stack anywhere in the loop.</p></div>
          <div><span className="label">Grip</span><h3>Fin-ray fingers</h3><p>Hollow triangles with five fanned ribs. The contact wall bows inward under load and the tip swings in with it, so the finger closes around the tube rather than onto it.</p></div>
          <div><span className="label">Eyes</span><h3>38° gripper mount</h3><p>One printed piece that slides onto the gripper block and stops against the frame. Holds the camera out over the fingertips, where the vial lip sits 5° above frame centre.</p></div>
          <div><span className="label">Bench</span><h3>One-piece station</h3><p>Reservoir and measuring camera share a single tray, so moving the station never changes the calibration. Hand it to a judge and let them put it anywhere.</p></div>
        </div>
      </Section>

      <Section id="why" accent={tint(4, 8)} label="Why" title="Most lab errors happen while samples are handled. Small labs can’t afford the robots that fix it."
        intro="Built for research techs and grad students in small academic wet labs who aliquot samples by hand for hours each week.">
        <div className="tiles">
          <div><span className="label">Errors in handling</span><b>60–70%</b><small>of lab testing errors are pre-analytical · BLDE study</small></div>
          <div><span className="label">Small labs</span><b>≤10%</b><small>automated, under 100 tubes a day · DOTmed, CLP</small></div>
          <div><span className="label">Openings a year</span><b>22,600</b><small>lab technologists and technicians · BLS via AMN</small></div>
        </div>
      </Section>

      <Section id="agent" accent={tint(5, 8)} label="Safety" title="Claude plans the run. Code decides if it may run. A person says go."
        intro="Ask in plain words or by voice. Claude turns the request into moves, a checker in code refuses anything unsafe, and nothing moves until a named person approves the exact plan they just heard.">
        <div className="hw">
          <div><span className="label">Plan</span><h3>Claude asks, never guesses</h3><p>A missing volume or an odd reagent gets a question back, not a plan.</p></div>
          <div><span className="label">Check</span><h3>Rules in code</h3><p>Volumes, wells, rack order and capacity are checked before the read-back. A swapped tube is caught before any liquid moves.</p></div>
          <div><span className="label">Approve</span><h3>A named person</h3><p>By click or by voice, logged with the name. “Stop” wins at any moment, and the hardware e-stop is the real stop.</p></div>
          <div><span className="label">Record</span><h3>Every sample kept</h3><p>Runs live in Convex; every pour and the 100 Hz arm trace go to Tiger Data, where the arm’s own data tuned its motors.</p></div>
        </div>
      </Section>

      <Section id="live" accent={tint(6, 8)} label="Live link" title="Watch a bench that isn’t yours."
        intro="The laptop at the bench publishes joint angles, gripper state and pour progress continuously. This page shows the same arm, from the same data.">
        <LiveLink />
      </Section>

      <section className="block tinted" id="team" data-reveal style={{ "--accent": tint(7, 8) } as CSSProperties}>
        <div className="wrap between">
          <div>
            <p className="label sec-label">Laminar</p>
            <p style={{ fontSize: "1.375rem", marginTop: 6, letterSpacing: "-0.02em" }}>Hema · Avery · Amogh · Om</p>
          </div>
          <p className="muted">Cal Hacks 13.0 · Seeed reBot B601-RS · 36 hours</p>
        </div>
      </section>
    </main>
  );
}
