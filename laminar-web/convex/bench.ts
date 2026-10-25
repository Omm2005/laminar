import { v } from "convex/values";
import { internalMutation, mutation, query, type MutationCtx } from "./_generated/server";
import { log } from "./runs";
import { LOW_TUBE_ML, TUBES, WELLS, WELL_CAPACITY_ML } from "./lib";

const TUBE_SPEC = {
  A: { stands: "Blood sample", dye: "#c8213a", fill: 5.0 },
  B: { stands: "Reagent 1", dye: "#1f57d6", fill: 2.0 },
  C: { stands: "Reagent 2", dye: "#e0b416", fill: 2.0 },
  D: { stands: "Reagent 3", dye: "#1f9a55", fill: 2.0 },
} as const;

/** Tubes and wells as the bench cameras see them. */
export const state = query({
  args: {},
  handler: async (ctx) => {
    const tubes = (await ctx.db.query("tubes").collect()).sort((a, b) => a.slot - b.slot);
    const wells = (await ctx.db.query("wells").collect()).sort((a, b) => a.number - b.number);
    const running = await ctx.db.query("runs").withIndex("by_status", (q) => q.eq("status", "running")).first();
    return { tubes, wells, running, lowMl: LOW_TUBE_ML, wellCapacityMl: WELL_CAPACITY_ML };
  },
});

async function seed(ctx: MutationCtx) {
  const now = Date.now();
  if ((await ctx.db.query("tubes").collect()).length === 0) {
    for (const [i, letter] of TUBES.entries()) {
      const s = TUBE_SPEC[letter];
      await ctx.db.insert("tubes", { slot: i + 1, letter, stands: s.stands, dye: s.dye, volumeMl: s.fill, fillLineMl: s.fill, updatedAt: now });
    }
  }
  if ((await ctx.db.query("wells").collect()).length === 0) {
    for (const n of WELLS) await ctx.db.insert("wells", { number: n, volumeMl: 0, capacityMl: WELL_CAPACITY_ML, contents: [], updatedAt: now });
  }
}

export const init = internalMutation({ args: {}, handler: async (ctx) => seed(ctx) });

/** Public so a fresh deployment fills itself on first page load. Idempotent. */
export const ensure = mutation({ args: {}, handler: async (ctx) => seed(ctx) });

async function idle(ctx: MutationCtx) {
  if (await ctx.db.query("runs").withIndex("by_status", (q) => q.eq("status", "running")).first())
    throw new Error("A run is in progress. Stop it before touching the bench.");
}

/** Demo of the caught mistake: physically swapping two tubes in the rack. */
export const swap = mutation({
  args: { a: v.number(), b: v.number(), by: v.string() },
  handler: async (ctx, { a, b, by }) => {
    await idle(ctx);
    const ta = await ctx.db.query("tubes").withIndex("by_slot", (q) => q.eq("slot", a)).first();
    const tb = await ctx.db.query("tubes").withIndex("by_slot", (q) => q.eq("slot", b)).first();
    if (!ta || !tb) return;
    await ctx.db.patch(ta._id, { slot: b, updatedAt: Date.now() });
    await ctx.db.patch(tb._id, { slot: a, updatedAt: Date.now() });
    await log(ctx, by.trim() || "bench", "swap", `Swapped the tubes in slots ${a} and ${b} (${ta.letter} ↔ ${tb.letter}).`);
  },
});

export const resetRack = mutation({
  args: { by: v.string() },
  handler: async (ctx, { by }) => {
    await idle(ctx);
    for (const t of await ctx.db.query("tubes").collect()) await ctx.db.patch(t._id, { slot: TUBES.indexOf(t.letter) + 1 });
    await log(ctx, by.trim() || "bench", "rack", "Put tubes back in order A, B, C, D.");
  },
});

export const refill = mutation({
  args: { by: v.string() },
  handler: async (ctx, { by }) => {
    await idle(ctx);
    for (const t of await ctx.db.query("tubes").collect()) await ctx.db.patch(t._id, { volumeMl: t.fillLineMl, updatedAt: Date.now() });
    await log(ctx, by.trim() || "bench", "refill", "Refilled every tube to its fill line.");
  },
});

export const emptyTray = mutation({
  args: { by: v.string() },
  handler: async (ctx, { by }) => {
    await idle(ctx);
    for (const w of await ctx.db.query("wells").collect()) await ctx.db.patch(w._id, { volumeMl: 0, contents: [], updatedAt: Date.now() });
    await log(ctx, by.trim() || "bench", "empty", "Emptied and rinsed wells 1–4.");
  },
});

export const auditLog = query({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, { limit }) => ctx.db.query("audit").withIndex("by_t").order("desc").take(Math.min(limit ?? 60, 300)),
});
