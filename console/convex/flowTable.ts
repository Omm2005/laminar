import { mutation, query, internalMutation } from "./_generated/server";
import { v } from "convex/values";

/** Return the singleton flow-table row (or null if not seeded yet). */
export const get = query({
  args: {},
  handler: async (ctx) => await ctx.db.query("flowTable").first(),
});

/** Overwrite the singleton flow-table (idempotent). */
export const update = mutation({
  args: {
    onsetDeg: v.number(),
    pourDeg: v.number(),
    rates: v.array(v.object({ deg: v.number(), mlPerS: v.number() })),
    updatedBy: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const existing = await ctx.db.query("flowTable").first();
    const row = {
      onsetDeg: args.onsetDeg,
      pourDeg: args.pourDeg,
      rates: args.rates,
      updatedAt: Date.now(),
      updatedBy: args.updatedBy ?? "manual",
    };
    if (existing) await ctx.db.patch(existing._id, row);
    else await ctx.db.insert("flowTable", row);
  },
});

/** Called by the cron: refit `rates` from the last 20 pours.
    Keeps the old rate for any angle with fewer than 3 points. */
export const refitInternal = internalMutation({
  args: {},
  handler: async (ctx) => {
    const existing = await ctx.db.query("flowTable").first();
    if (!existing) return; // nothing to refit against
    const pours = await ctx.db.query("pours")
      .withIndex("by_createdAt").order("desc").take(20);
    if (pours.length < 3) return;

    // bucket by pourDeg
    const buckets = new Map<number, number[]>();
    for (const p of pours) {
      if (!Number.isFinite(p.measuredMlPerS) || p.measuredMlPerS <= 0) continue;
      const deg = Math.round(p.pourDeg);
      if (!buckets.has(deg)) buckets.set(deg, []);
      buckets.get(deg)!.push(p.measuredMlPerS);
    }

    const oldByDeg = new Map(existing.rates.map((r) => [Math.round(r.deg), r.mlPerS]));
    const newRates: { deg: number; mlPerS: number }[] = [];
    const allDegs = new Set<number>([...oldByDeg.keys(), ...buckets.keys()]);
    for (const deg of [...allDegs].sort((a, b) => a - b)) {
      const samples = buckets.get(deg) ?? [];
      if (samples.length >= 3) {
        const mean = samples.reduce((s, v) => s + v, 0) / samples.length;
        newRates.push({ deg, mlPerS: mean });
      } else if (oldByDeg.has(deg)) {
        newRates.push({ deg, mlPerS: oldByDeg.get(deg)! });
      }
    }
    await ctx.db.patch(existing._id, {
      rates: newRates,
      updatedAt: Date.now(),
      updatedBy: "cron-refit",
    });
  },
});

/** Idempotent seed used by scripts/seed.mjs. */
export const seedInternal = internalMutation({
  args: {
    onsetDeg: v.number(),
    pourDeg: v.number(),
    rates: v.array(v.object({ deg: v.number(), mlPerS: v.number() })),
  },
  handler: async (ctx, args) => {
    const existing = await ctx.db.query("flowTable").first();
    const row = {
      onsetDeg: args.onsetDeg,
      pourDeg: args.pourDeg,
      rates: args.rates,
      updatedAt: Date.now(),
      updatedBy: "seed",
    };
    if (existing) await ctx.db.patch(existing._id, row);
    else await ctx.db.insert("flowTable", row);
  },
});
