/** Shared bench rules and plan helpers. Plain code, no AI: the checker is what decides if a plan may run. */
import type { Doc } from "./_generated/dataModel";

export type Tube = "A" | "B" | "C" | "D";
export type Task = { tube: Tube; pours: { well: number; ml: number }[] };

export const TUBES: Tube[] = ["A", "B", "C", "D"];
export const WELLS = [1, 2, 3, 4];
export const CONTROL_WELL = 4;
export const WELL_CAPACITY_ML = 3.0;
export const MAX_POUR_ML = 2.0;
export const MIN_POUR_ML = 0.2;
export const MAX_A_ALIQUOTS = 4;     // tube A is used for at most four aliquots per fill
export const BAND = 0.1;             // a pour is in band within ±10% of its target
export const LOW_TUBE_ML = 1.0;

/** The standard assay from the plan: A into all four wells, B/C/D into wells 1/2/3, well 4 is the control. */
export const STANDARD_ASSAY: Task[] = [
  { tube: "A", pours: [{ well: 1, ml: 1.0 }, { well: 2, ml: 1.0 }, { well: 3, ml: 1.0 }, { well: 4, ml: 1.0 }] },
  { tube: "B", pours: [{ well: 1, ml: 0.5 }] },
  { tube: "C", pours: [{ well: 2, ml: 0.5 }] },
  { tube: "D", pours: [{ well: 3, ml: 0.5 }] },
];

/** grab → lift(10) → [travel_to → lower(6) → pour → lift(6)]×n → travel_to(slot) → lower(10) → grab reversed */
export function expand(tasks: Task[]): string[] {
  const moves: string[] = [];
  for (const t of tasks) {
    moves.push(`grab_${t.tube}`, "lift(10)");
    for (const p of t.pours) {
      moves.push(`travel_to(well${p.well})`, "lower(6)", `pour(well${p.well}, ${p.ml.toFixed(1)} mL)`, "lift(6)");
    }
    moves.push(`travel_to(slot_${t.tube})`, "lower(10)", `release_${t.tube}`);
  }
  return moves;
}

/** Checker rules, in code. Returns a list of human-readable errors; empty means the plan may be read back. */
export function check(tasks: Task[], tubes: Doc<"tubes">[], wells: Doc<"wells">[]): string[] {
  const errors: string[] = [];
  if (tasks.length === 0) errors.push("The plan has no moves.");

  // The camera's letter reading must match the expected rack order A, B, C, D.
  const bySlot = [...tubes].sort((a, b) => a.slot - b.slot);
  const wrong = bySlot.filter((t, i) => t.letter !== TUBES[i]);
  if (wrong.length > 0) {
    const desc = wrong.map((t) => `slot ${t.slot} holds ${t.letter}`).join(", ");
    errors.push(`The rack doesn't match the plan: ${desc}. Expected A, B, C, D from left to right. Swap them back before any reagent goes in the wrong well.`);
  }

  const added = new Map<number, number>();
  const seen = new Set<Tube>();
  for (const t of tasks) {
    if (!TUBES.includes(t.tube)) { errors.push(`Unknown tube "${t.tube}".`); continue; }
    if (seen.has(t.tube)) errors.push(`Tube ${t.tube} is picked up twice. Merge its pours into one task.`);
    seen.add(t.tube);
    if (t.pours.length === 0) errors.push(`Tube ${t.tube} is picked up but never poured.`);
    if (t.tube === "A" && t.pours.length > MAX_A_ALIQUOTS) errors.push(`Tube A can only be split into ${MAX_A_ALIQUOTS} aliquots per fill; the plan has ${t.pours.length}.`);
    const tube = tubes.find((x) => x.letter === t.tube);
    const need = t.pours.reduce((s, p) => s + p.ml, 0);
    if (tube && need > tube.volumeMl + 1e-9) errors.push(`Tube ${t.tube} holds ${tube.volumeMl.toFixed(1)} mL but the plan pours ${need.toFixed(1)} mL. Refill it to the line.`);
    for (const p of t.pours) {
      if (!WELLS.includes(p.well)) errors.push(`Well ${p.well} doesn't exist. Wells are 1 to 4.`);
      if (!(p.ml >= MIN_POUR_ML && p.ml <= MAX_POUR_ML)) errors.push(`${p.ml} mL into well ${p.well} is outside the ${MIN_POUR_ML}–${MAX_POUR_ML} mL a single pour can do.`);
      if (p.well === CONTROL_WELL && t.tube !== "A") errors.push(`Well ${CONTROL_WELL} is the control and only gets sample A. Tube ${t.tube} can't go there.`);
      added.set(p.well, (added.get(p.well) ?? 0) + p.ml);
    }
  }
  for (const [well, ml] of added) {
    const w = wells.find((x) => x.number === well);
    const now = w?.volumeMl ?? 0;
    if (now + ml > WELL_CAPACITY_ML + 1e-9) errors.push(`Well ${well} would hold ${(now + ml).toFixed(1)} mL; it fits ${WELL_CAPACITY_ML.toFixed(1)} mL. Empty the tray or pour less.`);
  }
  // Order: sample A must go into a well before a reagent does, so reagents land on sample.
  const aIdx = tasks.findIndex((t) => t.tube === "A");
  tasks.forEach((t, i) => {
    if (t.tube !== "A" && aIdx > i) {
      for (const p of t.pours) if (tasks[aIdx].pours.some((q) => q.well === p.well)) {
        errors.push(`Reagent ${t.tube} goes into well ${p.well} before sample A. Pour the sample first.`);
      }
    }
  });
  return [...new Set(errors)];
}

/** Spoken-style numbers: "one and a half", "two", "0.5". */
const WORDS: Record<string, number> = { half: 0.5, one: 1, two: 2, three: 3, four: 4, a: 1, an: 1 };
function num(s: string): number | null {
  s = s.trim().toLowerCase();
  const m = s.match(/^(\d+(?:\.\d+)?)$/);
  if (m) return parseFloat(m[1]);
  const andHalf = s.match(/^(\w+) and a half$/);
  if (andHalf && WORDS[andHalf[1]] !== undefined) return WORDS[andHalf[1]] + 0.5;
  if (s === "half" || s === "a half") return 0.5;
  return WORDS[s] ?? null;
}

/**
 * Deterministic planner used when no ANTHROPIC_API_KEY is set, and as a safety net.
 * Understands "run the assay", "pour 1 ml of A into well 2", "only test reagent C on two wells".
 */
export function rulePlan(text: string): { tasks?: Task[]; question?: string; note: string } {
  let s = text.toLowerCase().replace(/millilit(er|re)s?|mls?\b/g, "ml");
  if (/\b(run|start|do)\b.*\bassay\b|\bstandard\b/.test(s) && !/only/.test(s)) {
    return { tasks: STANDARD_ASSAY, note: "Standard assay: A into wells 1–4, then B, C, D into wells 1, 2, 3. Well 4 is the control." };
  }
  s = s.replace(/\b(red|blood)\b/g, "a").replace(/\bblue\b/g, "b").replace(/\byellow\b/g, "c").replace(/\bgreen\b/g, "d");
  const only = s.match(/only (?:test )?(?:reagent )?([bcd])\b(?: on| in| into)? (\w+) wells?/);
  if (only) {
    const tube = only[1].toUpperCase() as Tube;
    const n = Math.min(3, Math.max(1, num(only[2]) ?? 1));
    const wells = [1, 2, 3].slice(0, n);
    return {
      tasks: [
        { tube: "A", pours: [...wells, CONTROL_WELL].map((w) => ({ well: w, ml: 1.0 })) },
        { tube, pours: wells.map((w) => ({ well: w, ml: 0.5 })) },
      ],
      note: `Reagent ${tube} on ${n} well${n > 1 ? "s" : ""} (${wells.join(", ")}), sample A in each plus the control.`,
    };
  }
  // The Mana console addresses wells as reservoir channels 1, 4, 7, 10 and has no tube field.
  const chan = s.match(/pour ([\w. ]+?) ?ml (?:of (?:tube )?([abcd]) )?(?:in|into|to) channel (\d{1,2})\b/);
  if (chan) {
    const well = { 1: 1, 4: 2, 7: 3, 10: 4 }[parseInt(chan[3])];
    const ml = num(chan[1]);
    if (!well) return { question: `Channel ${chan[3]} is a splash channel. Wells are channels 1, 4, 7 and 10.`, note: "Channel is not a well." };
    if (ml === null) return { question: `How many milliliters should go into well ${well}?`, note: "Volume unclear." };
    if (!chan[2]) return { question: `Which tube should pour ${ml} mL into well ${well} (channel ${chan[3]})? Say A, B, C or D.`, note: "No tube given; not guessing one." };
    return { tasks: [{ tube: chan[2].toUpperCase() as Tube, pours: [{ well, ml }] }], note: `${ml} mL of ${chan[2].toUpperCase()} into well ${well} (channel ${chan[3]}).` };
  }
  const pours = [...s.matchAll(/pour ([\w. ]+?) ?ml of (?:tube )?([abcd])\b (?:in|into|to) (?:well )?(\d)/g)];
  if (pours.length > 0) {
    const tasks: Task[] = [];
    for (const p of pours) {
      const ml = num(p[1]);
      if (ml === null) return { question: `How many milliliters should go into well ${p[3]}?`, note: "Volume unclear." };
      const tube = p[2].toUpperCase() as Tube;
      const existing = tasks.find((t) => t.tube === tube);
      (existing ?? tasks[tasks.push({ tube, pours: [] }) - 1]).pours.push({ well: parseInt(p[3]), ml });
    }
    tasks.sort((a, b) => (a.tube === "A" ? -1 : b.tube === "A" ? 1 : 0));
    return { tasks, note: `${pours.length} pour${pours.length > 1 ? "s" : ""} as asked.` };
  }
  if (/pour|add|put/.test(s)) return { question: "Which tube, how many milliliters, and into which well? For example: pour 1 ml of A into well 2.", note: "Request incomplete." };
  return { question: "I can run the standard assay, a custom set of pours, or a single reagent test. What would you like?", note: "Request not understood." };
}

export const r2 = (x: number) => Math.round(x * 100) / 100;
