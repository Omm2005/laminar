import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";
import type { Doc } from "../../convex/_generated/dataModel";
import { type Station, errText, useStation } from "../station";

/** The Bench: everything the robot needs, at a glance and editable. What is connected (with the action that fixes
 *  it), the containers the side camera sees and the vials in the rack (names are for people only), and what is
 *  still to set up. The camera picture only shows while calibrating. */

type Live = Station & { connected: true; arm: { connected: boolean; detail: string } };
type Box = [number, number, number, number];

export default function Bench() {
  const st = useStation();
  const live = st?.connected && "arm" in st ? (st as Live) : null;
  const [calibrating, setCalibrating] = useState(false);
  return (
    <>
      <section className="x-hero x-wash">
        <span className="x-eyebrow">Bench</span>
        <h1 className="x-h1">Everything the robot needs.<br /><mark>Set up once, check at a glance.</mark></h1>
        <p className="x-lede">What is connected, what the side camera sees in each container, and which vials are in the rack.
          Names here are for people; the arm only uses positions.</p>
      </section>

      <section className="x-section tight">
        <StatusGrid st={st} live={live} onCalibrate={() => setCalibrating(true)} calibrating={calibrating} />
      </section>

      {calibrating && live && (
        <section className="x-section">
          <Calibrator live={live} onDone={() => setCalibrating(false)} />
        </section>
      )}

      <section className="x-section">
        <div className="bench-cols">
          <Containers live={live} />
          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
            <Vials live={live} />
            <Checklist live={live} />
          </div>
        </div>
      </section>
    </>
  );
}

/* ------------------------------------------------------------------ status */

function StatusGrid({ st, live, onCalibrate, calibrating }: { st: ReturnType<typeof useStation>; live: Live | null;
                                                            onCalibrate: () => void; calibrating: boolean }) {
  const setCamera = useMutation(api.station.setCamera);
  const setup = useMutation(api.station.requestSetup);
  const [err, setErr] = useState<string | null>(null);
  const [asked, setAsked] = useState(false);
  const cmd = live?.lastCommand;
  const working = cmd?.status === "pending" || cmd?.status === "running";
  const off = !st?.connected;
  const cell = (label: string, ok: boolean, warn: boolean, value: string, detail: string, act?: React.ReactNode) => (
    <div className="status-cell">
      <span className="lbl"><i className={`x-dot ${ok ? "ok" : warn ? "warn" : "bad"}`} />{label}</span>
      <span className="val">{value}</span>
      <span className="det" title={detail}>{detail}</span>
      {act && <div className="act">{act}</div>}
    </div>
  );
  return (
    <>
      <div className="status-grid">
        {cell("Robot arm", !!live?.arm.connected, false, off ? "Offline" : live?.arm.connected ? "Connected" : "Not connected",
              off ? "The bench computer is not connected yet." : live?.arm.detail ?? "",
              !live?.arm.connected && !off ? <span className="x-small x-muted mono">start arm_server.py</span> : undefined)}
        {cell("Side camera", !!live?.camera.connected, false, live?.camera.connected ? `Camera ${live.camIndex ?? "0"}` : "Not connected",
              live?.camera.detail ?? "not connected yet",
              live && !live.simulated ? (
                <select className="x-input" style={{ height: 30 }} value={live.camIndex ?? "0"} aria-label="Side camera"
                        disabled={working || !!live.busy}
                        onChange={async (e) => { setErr(null); try { await setCamera({ index: e.target.value }); } catch (x) { setErr(errText(x)); } }}>
                  {["0", "1", "2"].map((i) => <option key={i} value={i}>Camera {i}</option>)}
                </select>) : undefined)}
        {cell("Level model", !!live?.model.loaded, false, live?.model.loaded ? "Loaded" : "Not loaded",
              live?.model.detail ?? "not connected yet",
              live?.readHz ? <span className="x-tag">{Math.round(live.readHz)} readings/s</span> : undefined)}
        {cell("Calibration", !!live?.calibration.done, !!live, live?.calibration.done ? `${live.containers.length} containers` : "Left to do",
              live?.calibration.detail ?? "not connected yet",
              live && !live.simulated ? (
                <>
                  <button className={`x-btn sm ${live.calibration.done ? "" : "primary"}`} onClick={onCalibrate}
                          disabled={!live.camera.connected || working || !!live.busy || calibrating}>
                    {live.calibration.done ? "Recalibrate" : "Calibrate"}
                  </button>
                  {live.calibration.done && (
                    <button className="x-btn sm" disabled={asked} title="Claude names the containers and checks the boxes (answer in the Lab chat)"
                            onClick={async () => { setAsked(true); try { await setup({}); } finally { setTimeout(() => setAsked(false), 8000); } }}>
                      {asked ? "Claude is looking…" : "Set up with Claude"}
                    </button>
                  )}
                </>) : undefined)}
        {cell("Recorded grabs", (live?.grabs?.length ?? 0) > 0, !!live, `${live?.grabs?.length ?? 0} vials`,
              live?.grabs?.length ? `Recorded: ${live.grabs.join(", ")}. Others are placeholders.` : "None recorded: picks are placeholders.")}
      </div>
      {err && <p className="x-small" style={{ color: "var(--bad)", marginTop: 10 }}>{err}</p>}
    </>
  );
}

/* ------------------------------------------------------------------ calibration (camera shown only here) */

function LiveVideo({ live, alt }: { live: Live; alt: string }) {
  const [failed, setFailed] = useState(false);
  const port = live.streamPort;
  useEffect(() => setFailed(false), [port, live.camIndex]);
  const src = port && !failed ? `${location.protocol}//${location.hostname}:${port}/raw.mjpg?cam=${live.camIndex ?? ""}` : live.rawFrameUrl;
  if (!src) return <div className="x-panel-body x-muted">No picture: the side camera is not connected yet.</div>;
  return <img src={src} alt={alt} draggable={false} onError={() => setFailed(true)} />;
}

/** Drag one box per container on the live camera (rim to floor, inside the walls). Numbered left to right =
 *  container 1, 2, ...; the bench computer saves them, then Claude checks them from the picture. */
function Calibrator({ live, onDone }: { live: Live; onDone: () => void }) {
  const save = useMutation(api.station.saveCalibration);
  const W = live.frameW ?? 1280, H = live.frameH ?? 720;
  const [boxes, setBoxes] = useState<Box[]>(() => (live.boxes ?? []).map((b) => b.slice(0, 4) as Box));
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const svg = useRef<SVGSVGElement>(null);
  const sorted = boxes.map((b, i) => ({ b, i })).sort((p, q) => p.b[0] - q.b[0]);

  const toImg = (e: React.PointerEvent) => {
    const r = svg.current!.getBoundingClientRect();
    return { x: Math.max(0, Math.min(W, ((e.clientX - r.left) / r.width) * W)), y: Math.max(0, Math.min(H, ((e.clientY - r.top) / r.height) * H)) };
  };
  function up() {
    if (!drag) return;
    const x = Math.min(drag.x0, drag.x1), y = Math.min(drag.y0, drag.y1), w = Math.abs(drag.x1 - drag.x0), h = Math.abs(drag.y1 - drag.y0);
    setDrag(null);
    if (w >= 6 && h >= 12) setBoxes((bs) => [...bs, [Math.round(x), Math.round(y), Math.round(w), Math.round(h)]]);
  }
  async function onSave() {
    setBusy(true); setErr(null);
    try { await save({ boxes }); onDone(); } catch (e) { setErr(errText(e)); } finally { setBusy(false); }
  }
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === "INPUT") return;
      if ((e.key === "Delete" || e.key === "Backspace") && sel !== null) { setBoxes((bs) => bs.filter((_, i) => i !== sel)); setSel(null); }
      if (e.key === "Escape") onDone();
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [sel, onDone]);

  return (
    <div className="x-panel">
      <div className="x-panel-head"><span>Calibrate the side camera</span><span className="x-grow" />
        <span className="x-tag num">{W}×{H}</span><span className="x-tag info">{boxes.length} boxes</span></div>
      <div className="x-panel-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <p className="x-small x-muted" style={{ margin: 0 }}>Drag a box around each container: top edge on the rim, bottom edge on the floor,
          sides just inside the walls. Numbered left to right = container 1, 2, … Click a box, then Delete to remove it.</p>
        <div className="cal-stage2">
          <LiveVideo live={live} alt="Live side camera" />
          <svg ref={svg} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
               onPointerDown={(e) => { const p = toImg(e); (e.target as Element).setPointerCapture?.(e.pointerId); setSel(null); setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y }); }}
               onPointerMove={(e) => { if (drag) { const p = toImg(e); setDrag({ ...drag, x1: p.x, y1: p.y }); } }}
               onPointerUp={up}>
            {sorted.map(({ b, i }, n) => (
              <g key={i}>
                <rect x={b[0]} y={b[1]} width={b[2]} height={b[3]} className={`cal-box ${sel === i ? "cal-sel" : ""}`}
                      onPointerDown={(e) => { e.stopPropagation(); setSel(i); }} />
                <text x={b[0] + 2} y={Math.max(16, b[1] - 6)} className="cal-label">{n + 1}</text>
              </g>
            ))}
            {drag && <rect x={Math.min(drag.x0, drag.x1)} y={Math.min(drag.y0, drag.y1)} width={Math.abs(drag.x1 - drag.x0)}
                           height={Math.abs(drag.y1 - drag.y0)} className="cal-drag" />}
          </svg>
        </div>
        {err && <div className="x-small" style={{ color: "var(--bad)" }}>{err}</div>}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <button className="x-btn primary" disabled={busy || !boxes.length} onClick={onSave}>Save calibration</button>
          <button className="x-btn" disabled={sel === null} onClick={() => { setBoxes(boxes.filter((_, i) => i !== sel)); setSel(null); }}>Delete selected</button>
          <button className="x-btn" disabled={!boxes.length} onClick={() => { setBoxes([]); setSel(null); }}>Clear all</button>
          <button className="x-btn" onClick={onDone}>Cancel</button>
          <span className="x-small x-muted" style={{ marginLeft: "auto" }}>After saving, Claude checks the boxes and names the containers (Lab chat).</span>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ containers + vials */

function Containers({ live }: { live: Live | null }) {
  const channels = useQuery(api.channels.list, {});
  const rename = useMutation(api.channels.rename);
  const seen = new Map((live?.containers ?? []).map((c) => [c.number, c]));
  return (
    <div className="x-panel">
      <div className="x-panel-head"><span>Containers · seen by the camera</span><span className="x-grow" />
        <span className="x-tag">{(channels ?? []).length}</span></div>
      {(channels ?? []).length === 0 ? (
        <div className="x-panel-body x-muted x-small">None yet: {live?.calibration.done ? "waiting for readings" : "calibrate the side camera to add them"}.</div>
      ) : (
        <table className="x-table">
          <thead><tr><th style={{ width: 36 }}>#</th><th>Name</th><th>Level</th><th style={{ width: 120 }}>mL</th><th style={{ width: 96 }}>Arm</th></tr></thead>
          <tbody>
            {(channels ?? []).map((c) => {
              const r = seen.get(c.number);
              const unsure = !!r && r.conf < 0.5;
              return (
                <tr key={c._id}>
                  <td className="mono x-muted">{String(c.number).padStart(2, "0")}</td>
                  <td><CellText value={c.contents} label={`Name of container ${c.number}`} onSave={(v) => rename({ number: c.number, contents: v })} /></td>
                  <td><div className="x-bar"><i style={{ width: `${Math.min(100, (100 * c.currentMl) / (c.capacityMl || 1))}%` }} /></div></td>
                  <td className="mono num">{unsure ? <span className="x-tag warn">unsure</span> : `${c.currentMl.toFixed(2)} / ${c.capacityMl}`}</td>
                  <td>{r ? (r.reachable ? <span className="x-tag ok">reachable</span>
                                         : <span className="x-tag warn" title="No taught pour spot over this container (layout.yaml pour_spots)">no spot</span>)
                         : <span className="x-tag">—</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Vials({ live }: { live: Live | null }) {
  const vials = useQuery(api.vials.list, {}) as Doc<"vials">[] | undefined;
  const upsert = useMutation(api.vials.upsert);
  const remove = useMutation(api.vials.remove);
  const used = new Set((vials ?? []).map((v) => v.slot));
  const next = [1, 2, 3, 4].find((s) => !used.has(s));
  const grabs = new Set(live?.grabs ?? []);
  return (
    <div className="x-panel">
      <div className="x-panel-head"><span>Vials · in the rack</span><span className="x-grow" />
        {next && <button className="x-btn sm" onClick={() => upsert({ slot: next, letter: String.fromCharCode(64 + next),
                                                                       contents: `Vial ${String.fromCharCode(64 + next)}`, volumeMl: 8 })}>
          + Add vial {String.fromCharCode(64 + next)}</button>}
      </div>
      {(vials ?? []).length === 0 ? (
        <div className="x-panel-body x-muted x-small">No vials yet. Add the ones in the rack (A–D).</div>
      ) : (
        <table className="x-table">
          <thead><tr><th style={{ width: 36 }}></th><th>Holds</th><th style={{ width: 96 }}>mL</th><th style={{ width: 104 }}>Grab</th><th style={{ width: 40 }}></th></tr></thead>
          <tbody>
            {(vials ?? []).map((v) => (
              <tr key={v._id}>
                <td className="mono">{v.letter}</td>
                <td><CellText value={v.contents} label={`What vial ${v.letter} holds`}
                              onSave={(t) => upsert({ slot: v.slot, letter: v.letter, contents: t, volumeMl: v.volumeMl })} /></td>
                <td><CellNumber value={v.volumeMl} label={`mL in vial ${v.letter}`}
                                onSave={(n) => upsert({ slot: v.slot, letter: v.letter, contents: v.contents, volumeMl: n })} /></td>
                <td>{grabs.has(`vial_${v.letter}`) ? <span className="x-tag ok">recorded</span> : <span className="x-tag warn">placeholder</span>}</td>
                <td><button className="x-icon-btn" aria-label={`Remove vial ${v.letter}`} title="Remove" onClick={() => remove({ slot: v.slot })}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 6l12 12M18 6L6 18" /></svg>
                </button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Checklist({ live }: { live: Live | null }) {
  const items: { ok: boolean; text: string }[] = [
    { ok: !!live, text: live ? "Bench computer connected" : "Start the dashboard (start.sh) on the bench computer" },
    { ok: !!live?.arm.connected, text: live?.arm.connected ? "Robot arm connected" : "Start arm_server.py (motors on)" },
    { ok: !!live?.camera.connected, text: live?.camera.connected ? "Side camera streaming" : "Plug in the side camera" },
    { ok: !!live?.calibration.done, text: live?.calibration.done ? "Side camera calibrated" : "Calibrate the side camera" },
    ...(live?.notReady ?? []).map((t) => ({ ok: false, text: t })),
  ];
  const left = items.filter((i) => !i.ok).length;
  return (
    <div className="x-panel">
      <div className="x-panel-head"><span>Setup checklist</span><span className="x-grow" />
        <span className={`x-tag ${left ? "warn" : "ok"}`}>{left ? `${left} left` : "ready"}</span></div>
      <div className="x-panel-body" style={{ paddingTop: 6, paddingBottom: 6 }}>
        <ul className="checklist">
          {items.map((i) => (
            <li key={i.text}><i className={`x-dot ${i.ok ? "ok" : "warn"}`} style={{ marginTop: 7 }} /><span className={i.ok ? "x-muted" : ""}>{i.text}</span></li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function CellText({ value, label, onSave }: { value: string; label: string; onSave: (v: string) => void }) {
  const [v, setV] = useState(value);
  useEffect(() => setV(value), [value]);
  const commit = () => { const t = v.trim(); if (t && t !== value) onSave(t); else setV(value); };
  return <input className="x-cell-input" aria-label={label} value={v} onChange={(e) => setV(e.target.value)} onBlur={commit}
                onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />;
}

function CellNumber({ value, label, onSave }: { value: number; label: string; onSave: (v: number) => void }) {
  const [v, setV] = useState(String(value));
  useEffect(() => setV(String(Math.round(value * 100) / 100)), [value]);
  const commit = () => { const n = Number(v); if (Number.isFinite(n) && n >= 0 && n !== value) onSave(n); else setV(String(value)); };
  return <input className="x-cell-input mono num" style={{ textAlign: "right" }} aria-label={label} inputMode="decimal" value={v}
                onChange={(e) => setV(e.target.value)} onBlur={commit} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />;
}
