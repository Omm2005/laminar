import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import type { Doc } from "../convex/_generated/dataModel";

export const DYE: Record<string, string> = { A: "var(--dye-a)", B: "var(--dye-b)", C: "var(--dye-c)", D: "var(--dye-d)" };
const HEX: Record<string, [number, number, number]> = { A: [200, 33, 58], B: [31, 87, 214], C: [217, 168, 12], D: [31, 154, 85] };
export const STANDS: Record<string, string> = { A: "Blood sample", B: "Reagent 1", C: "Reagent 2", D: "Reagent 3" };

/** Volume-weighted mix of the dyes in a well. */
export function mix(contents: { tube: string; ml: number }[]) {
  const tot = contents.reduce((s, c) => s + c.ml, 0);
  if (!tot) return "transparent";
  const rgb = [0, 1, 2].map((i) => Math.round(contents.reduce((s, c) => s + HEX[c.tube][i] * c.ml, 0) / tot));
  return `rgb(${rgb.join(",")})`;
}

export const fmtMl = (x: number | null | undefined, d = 2) => (x === null || x === undefined ? "—" : `${x.toFixed(d)} mL`);
export const fmtSigned = (x: number | null | undefined, d = 3) =>
  x === null || x === undefined ? "—" : `${x > 0 ? "+" : x < 0 ? "−" : "±"}${Math.abs(x).toFixed(d)}`;
export const ago = (t: number) => {
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(t).toLocaleDateString();
};
export const clock = (t: number) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** The operator's name, remembered in this browser. It goes into every audit entry. */
export function useName(): [string, (s: string) => void] {
  const [name, setName] = useState(() => {
    try { return localStorage.getItem("laminar.name") ?? ""; } catch { return ""; }
  });
  useEffect(() => { try { localStorage.setItem("laminar.name", name); } catch { /* private window */ } }, [name]);
  return [name, setName];
}

const STATUS: Record<string, [string, string]> = {
  checking: ["live", "checking"], rejected: ["bad", "refused"], awaiting_approval: ["warn", "awaiting approval"],
  approved: ["live", "approved"], running: ["live", "running"], done: ["ok", "done"], aborted: ["bad", "aborted"],
};
export function Status({ s }: { s: string }) {
  const [cls, text] = STATUS[s] ?? ["", s];
  return <span className={`chip ${cls}`}>{text}</span>;
}

/** Lime (top of the page) to sky blue (bottom): the section's accent for step i of n. */
export function tint(i: number, n: number) {
  const a = [195, 239, 176], b = [125, 211, 252];
  const f = n <= 1 ? 0 : i / (n - 1);
  return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(",")})`;
}

export function Section({ id, label, title, intro, children, accent }: { id: string; label: string; title: string; intro?: ReactNode; children: ReactNode; accent?: string }) {
  return (
    <section className="block tinted" id={id} data-reveal style={accent ? ({ "--accent": accent } as CSSProperties) : undefined}>
      <div className="wrap sec">
        <div className="side"><p className="label sec-label">{label}</p></div>
        <div className="stack">
          <div>
            <h2>{title}</h2>
            {intro && <p className="intro">{intro}</p>}
          </div>
          {children}
        </div>
      </div>
    </section>
  );
}

/** Adds .in to every [data-reveal] element as it scrolls into view (once). */
export function useReveal() {
  useEffect(() => {
    const els = [...document.querySelectorAll<HTMLElement>("[data-reveal]")];
    if (!("IntersectionObserver" in window) || matchMedia("(prefers-reduced-motion: reduce)").matches) {
      els.forEach((e) => e.classList.add("in"));
      return;
    }
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
    }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
    els.forEach((e) => io.observe(e));
    return () => io.disconnect();
  }, []);
}

// ---------------------------------------------------------------- bench drawing

/** Where the gripper is, from the move being played. */
function armSpot(move: string | undefined): { kind: "slot" | "well" | "home"; at?: string | number; tube?: string } {
  if (!move) return { kind: "home" };
  const w = move.match(/well(\d)/);
  if (w) return { kind: "well", at: parseInt(w[1]) };
  const s = move.match(/(?:grab|release|slot)_([ABCD])/);
  if (s) return { kind: "slot", at: s[1] };
  return { kind: "home" };
}

export function Bench({ tubes, wells, move, holding, pouring }: {
  tubes: Doc<"tubes">[]; wells: Doc<"wells">[]; move?: string; holding?: string; pouring?: boolean;
}) {
  const W = 640, H = 270, base = 222;
  const slotX = (slot: number) => 52 + (slot - 1) * 56;
  const wellX = (n: number) => 368 + (n - 1) * 66;
  const spot = armSpot(move);
  let ax = 300;
  if (spot.kind === "slot") ax = slotX(tubes.find((t) => t.letter === spot.at)?.slot ?? 1) + 14;
  if (spot.kind === "well") ax = wellX(spot.at as number) + 22;
  const lowered = !!move && /lower|pour|grab|release/.test(move);
  const ay = lowered ? 70 : 34;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Bench: tube rack on the left, wells 1 to 4 on the right" style={{ width: "100%", height: "auto", display: "block" }}>
      <line x1="16" x2={W - 16} y1={base + 1} y2={base + 1} stroke="var(--rule)" strokeWidth="2" />
      <text x="52" y={H - 12} fontSize="11" fill="var(--muted)">RACK · slots 1–4</text>
      <text x="368" y={H - 12} fontSize="11" fill="var(--muted)">TRAY · wells 1–4 (4 = control)</text>
      {/* rack */}
      <rect x="40" y={base - 18} width="232" height="18" rx="2" fill="var(--glass)" stroke="var(--rule)" />
      {tubes.map((t) => {
        const x = slotX(t.slot), h = 110, top = base - h - 6;
        const lifted = holding === t.letter;
        const frac = Math.min(1, t.volumeMl / t.fillLineMl);
        return (
          <g key={t._id} opacity={lifted ? 0.28 : 1}>
            <rect x={x} y={top} width="28" height={h} rx="5" fill="var(--glass)" stroke="var(--ink)" strokeWidth="1.3" />
            <rect x={x + 2} y={top + 2 + (h - 4) * (1 - frac)} width="24" height={(h - 4) * frac} rx="3" fill={DYE[t.letter]} opacity="0.85" />
            <line x1={x - 3} x2={x + 31} y1={top + 2} y2={top + 2} stroke="var(--muted)" strokeDasharray="2 2" />
            <text x={x + 14} y={top - 8} textAnchor="middle" fontSize="15" fontWeight="700" fill="var(--ink)" style={{ fontFamily: "var(--f-display)" }}>{t.letter}</text>
            <text x={x + 14} y={base + 15} textAnchor="middle" fontSize="10" fill="var(--muted)">{t.volumeMl.toFixed(1)}</text>
          </g>
        );
      })}
      {/* tray */}
      <rect x="356" y={base - 10} width="272" height="10" rx="2" fill="var(--glass)" stroke="var(--rule)" />
      {wells.map((w) => {
        const x = wellX(w.number), h = 70, top = base - h - 10;
        const frac = Math.min(1, w.volumeMl / w.capacityMl);
        return (
          <g key={w._id}>
            <path d={`M${x} ${top} v${h - 10} q0 10 10 10 h24 q10 0 10 -10 v${-(h - 10)}`} fill="var(--glass)" stroke={w.volumeMl > w.capacityMl * 0.9 ? "var(--warn)" : "var(--ink)"} strokeWidth="1.3" />
            {frac > 0 && <rect x={x + 2} y={top + (h - 2) * (1 - frac)} width="40" height={(h - 2) * frac} rx="7" fill={mix(w.contents)} opacity="0.9" />}
            <text x={x + 22} y={top - 8} textAnchor="middle" fontSize="13" fontWeight="600" fill="var(--ink)">{w.number}</text>
            <text x={x + 22} y={base + 15} textAnchor="middle" fontSize="10" fill="var(--muted)">{w.volumeMl.toFixed(2)}</text>
          </g>
        );
      })}
      {/* arm */}
      <g style={{ transition: "transform .45s ease" }} transform={`translate(${ax} ${ay})`}>
        <line x1="0" y1={-ay + 6} x2="0" y2="0" stroke="var(--muted)" strokeWidth="3" />
        <path d="M-12 0 h24 M-12 0 v10 M12 0 v10" stroke="var(--ink)" strokeWidth="3" fill="none" />
        {holding && (
          <g transform={pouring ? "rotate(-28)" : undefined} style={{ transition: "transform .3s" }}>
            <rect x="-8" y="4" width="16" height="44" rx="4" fill={DYE[holding]} opacity="0.85" stroke="var(--ink)" />
            <text x="0" y="31" textAnchor="middle" fontSize="11" fontWeight="700" fill="#fff">{holding}</text>
          </g>
        )}
      </g>
      <text x="16" y="18" fontSize="11" fill="var(--muted)">{move ? `▶ ${move}` : "arm at home"}</text>
    </svg>
  );
}

// ---------------------------------------------------------------- charts

/** Pour error as a share of target, one dot per pour, with the ±10% band shaded. */
export function ErrorChart({ pours }: { pours: { errorMl: number; targetMl: number; tube: string; t: number }[] }) {
  const W = 720, H = 240, L = 44, R = 12, T = 14, B = 28;
  const lim = Math.max(0.2, ...pours.map((p) => Math.abs(p.errorMl / p.targetMl))) * 1.1;
  const y = (r: number) => T + ((lim - r) / (2 * lim)) * (H - T - B);
  const x = (i: number) => L + (pours.length <= 1 ? (W - L - R) / 2 : (i / (pours.length - 1)) * (W - L - R));
  const ticks = [-0.2, -0.1, 0, 0.1, 0.2].filter((v) => Math.abs(v) <= lim);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Pour error as percent of target" style={{ width: "100%", height: "auto", display: "block" }}>
      <rect x={L} y={y(0.1)} width={W - L - R} height={y(-0.1) - y(0.1)} fill="var(--ok)" opacity="0.1" />
      {ticks.map((v) => (
        <g key={v}>
          <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--rule)" strokeDasharray={v === 0 ? undefined : "3 3"} />
          <text x={L - 6} y={y(v) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">{v > 0 ? "+" : ""}{Math.round(v * 100)}%</text>
        </g>
      ))}
      <text x={W - R} y={y(0.1) - 4} textAnchor="end" fontSize="10" fill="var(--ok)">±10% band</text>
      {pours.length > 1 && (
        <polyline fill="none" stroke="var(--muted)" strokeWidth="1" opacity="0.4" points={pours.map((p, i) => `${x(i)},${y(p.errorMl / p.targetMl)}`).join(" ")} />
      )}
      {pours.map((p, i) => (
        <circle key={i} cx={x(i)} cy={y(p.errorMl / p.targetMl)} r={i === pours.length - 1 ? 5 : 3.5} fill={DYE[p.tube]} stroke="var(--surface)" strokeWidth="1">
          <title>{`${p.tube} · ${fmtSigned(p.errorMl)} mL of ${p.targetMl.toFixed(1)} · ${clock(p.t)}`}</title>
        </circle>
      ))}
      <text x={L} y={H - 8} fontSize="10" fill="var(--muted)">oldest</text>
      <text x={W - R} y={H - 8} textAnchor="end" fontSize="10" fill="var(--muted)">latest pour</text>
    </svg>
  );
}

/** Before/after bars per joint. */
export function CompareBars({ rows, unit }: { rows: { joint: number; baseline: number; improved: number }[]; unit: string }) {
  const W = 640, rowH = 34, L = 40, R = 70, H = rows.length * rowH + 24;
  const max = Math.max(...rows.flatMap((r) => [r.baseline, r.improved])) * 1.05;
  const x = (v: number) => L + (v / max) * (W - L - R);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Before and after, in ${unit}`} style={{ width: "100%", height: "auto", display: "block" }}>
      {rows.map((r, i) => {
        const y0 = 6 + i * rowH;
        return (
          <g key={r.joint}>
            <text x={L - 8} y={y0 + 17} textAnchor="end" fontSize="12" fill="var(--ink)">J{r.joint}</text>
            <rect x={L} y={y0} width={x(r.baseline) - L} height="11" fill="var(--muted)" opacity="0.45" />
            <rect x={L} y={y0 + 13} width={x(r.improved) - L} height="11" fill="var(--accent)" />
            <text x={x(r.baseline) + 6} y={y0 + 10} fontSize="10" fill="var(--muted)">{r.baseline}</text>
            <text x={x(r.improved) + 6} y={y0 + 23} fontSize="10" fill="var(--accent)" fontWeight="500">{r.improved}</text>
          </g>
        );
      })}
      <g transform={`translate(${L} ${H - 8})`}>
        <rect width="10" height="8" y="-7" fill="var(--muted)" opacity="0.45" /><text x="14" fontSize="10" fill="var(--muted)">before</text>
        <rect x="64" width="10" height="8" y="-7" fill="var(--accent)" /><text x="78" fontSize="10" fill="var(--muted)">after (feedforward + smoothed grab), {unit}</text>
      </g>
    </svg>
  );
}


const VIA: Record<string, string> = { mana: "from Mana", voice: "voice", protocol: "protocol", web: "web", chat: "assistant" };
/** Where a run came from: the Mana Lab Console, Cartesia voice, a saved protocol, the assistant chat or the web form. */
export function Via({ v }: { v?: string }) {
  if (!v || v === "web") return null;
  return <span className={`chip via-${v}`}>{VIA[v] ?? v}</span>;
}

// ---------------------------------------------------------------- project page scroll effects

const mixHex = (a: string, b: string, f: number) => {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16)), pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  return `rgb(${pa.map((v, k) => Math.round(v + (pb[k] - v) * f)).join(",")})`;
};
/** Green-tinted charcoal at the top of the page to deep navy at the bottom. */
const THEME: [string, string, string][] = [
  ["--bg", "#111613", "#081221"],
  ["--surface", "#181e1a", "#0f1c30"],
  ["--surface-2", "#1d241f", "#13223a"],
  ["--rule", "#2c362e", "#1f3150"],
  ["--code-bg", "#141a16", "#0c1729"],
];

/** Sets the page colors from the scroll position. Restores the console theme when the page unmounts. */
export function useScrollTheme() {
  useEffect(() => {
    const root = document.documentElement;
    let frame = 0;
    const apply = () => {
      frame = 0;
      const max = Math.max(1, root.scrollHeight - innerHeight);
      const f = Math.min(1, Math.max(0, scrollY / max));
      for (const [name, a, b] of THEME) root.style.setProperty(name, mixHex(a, b, f));
    };
    const onScroll = () => { if (!frame) frame = requestAnimationFrame(apply); };
    apply();
    addEventListener("scroll", onScroll, { passive: true });
    addEventListener("resize", onScroll);
    return () => {
      removeEventListener("scroll", onScroll); removeEventListener("resize", onScroll);
      cancelAnimationFrame(frame);
      for (const [name] of THEME) root.style.removeProperty(name);
    };
  }, []);
}

/**
 * When a section scrolls into place, settle it at the top of the screen and hold the page still for
 * `ms`, once per section per visit. Navigation links (any in-page #anchor click) skip the hold.
 */
export function useSectionPause(ids: string[], ms = 1000) {
  useEffect(() => {
    const done = new Set<string>();
    let lockedUntil = 0, skipUntil = 0, lastY = scrollY;
    const header = () => (document.querySelector(".bar") as HTMLElement | null)?.offsetHeight ?? 0;
    const block = (e: Event) => {
      if (Date.now() >= lockedUntil) return;
      if (e instanceof KeyboardEvent && !["ArrowDown", "ArrowUp", "PageDown", "PageUp", " ", "Spacebar", "Home", "End"].includes(e.key)) return;
      if (e instanceof KeyboardEvent && ["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement).tagName)) return;
      e.preventDefault();
    };
    const onScroll = () => {
      const down = scrollY > lastY;
      lastY = scrollY;
      if (!down || Date.now() < lockedUntil || Date.now() < skipUntil) return;
      const top = header();
      for (const id of ids) {
        if (done.has(id)) continue;
        const el = document.getElementById(id);
        if (!el) continue;
        const r = el.getBoundingClientRect().top - top;
        if (r < innerHeight * 0.3 && r > -innerHeight * 0.25) {
          done.add(id);
          lockedUntil = Date.now() + ms;
          scrollTo({ top: scrollY + r, behavior: "smooth" });
          document.documentElement.classList.add("paused");
          setTimeout(() => document.documentElement.classList.remove("paused"), ms);
          break;
        }
      }
    };
    const onClick = (e: MouseEvent) => {
      const a = (e.target as HTMLElement).closest("a[href^='#']");
      if (a) { skipUntil = Date.now() + 1500; lockedUntil = 0; }
    };
    addEventListener("scroll", onScroll, { passive: true });
    for (const t of ["wheel", "touchmove"] as const) addEventListener(t, block, { passive: false });
    addEventListener("keydown", block);
    addEventListener("click", onClick, true);
    return () => {
      removeEventListener("scroll", onScroll);
      for (const t of ["wheel", "touchmove"] as const) removeEventListener(t, block);
      removeEventListener("keydown", block);
      removeEventListener("click", onClick, true);
      document.documentElement.classList.remove("paused");
    };
  }, [ids, ms]);
}
