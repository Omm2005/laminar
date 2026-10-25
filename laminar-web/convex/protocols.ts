/** Protocol Builder: save a plate map, import it from CSV (kept in file storage), and turn it into a run. */
import { v } from "convex/values";
import { mutation, query } from "./_generated/server";

import { TUBE } from "./schema";
import { check, expand, type Task } from "./lib";
import { log } from "./runs";
import { limiter } from "./limits";

const ROW = v.object({ tube: TUBE, well: v.number(), ml: v.number() });

export const list = query({
  args: { search: v.optional(v.string()) },
  handler: async (ctx, { search }) => {
    if (search?.trim()) return ctx.db.query("protocols").withSearchIndex("search_name", (q) => q.search("name", search)).take(20);
    return ctx.db.query("protocols").withIndex("by_updatedAt").order("desc").take(30);
  },
});

export const get = query({
  args: { id: v.id("protocols") },
  handler: async (ctx, { id }) => {
    const p = await ctx.db.get(id);
    if (!p) return null;
    return { ...p, sourceUrl: p.sourceFile ? await ctx.storage.getUrl(p.sourceFile) : null };
  },
});

/** Live validation of a draft against the bench as it is right now. */
export const validate = query({
  args: { rows: v.array(ROW) },
  handler: async (ctx, { rows }) => {
    const tasks = toTasks(rows);
    const errors = check(tasks, await ctx.db.query("tubes").collect(), await ctx.db.query("wells").collect());
    return { errors, moves: expand(tasks).length, seconds: Math.round(expand(tasks).length * 1.6) };
  },
});

export const generateUploadUrl = mutation({ args: {}, handler: async (ctx) => ctx.storage.generateUploadUrl() });

export const save = mutation({
  args: { id: v.optional(v.id("protocols")), name: v.string(), rows: v.array(ROW), by: v.string(), sourceFile: v.optional(v.id("_storage")), sourceName: v.optional(v.string()) },
  handler: async (ctx, a) => {
    const by = a.by.trim(); if (!by) throw new Error("Enter your name first.");
    const name = a.name.trim().slice(0, 80) || "Untitled protocol";
    const now = Date.now();
    if (a.id) {
      const cur = await ctx.db.get(a.id);
      if (!cur) throw new Error("That protocol was deleted.");
      await ctx.db.patch(a.id, { name, rows: a.rows, version: cur.version + 1, updatedAt: now, ...(a.sourceFile ? { sourceFile: a.sourceFile, sourceName: a.sourceName } : {}) });
      await log(ctx, by, "protocol", `Saved "${name}" v${cur.version + 1} (${a.rows.length} pours).`);
      return a.id;
    }
    const id = await ctx.db.insert("protocols", { name, rows: a.rows, version: 1, createdBy: by, updatedAt: now, sourceFile: a.sourceFile, sourceName: a.sourceName });
    await log(ctx, by, "protocol", `Created "${name}" (${a.rows.length} pours${a.sourceName ? `, from ${a.sourceName}` : ""}).`);
    return id;
  },
});

/** Send a saved protocol to the bench: it skips the planner but not the checker or the named approval. */
export const toRun = mutation({
  args: { id: v.id("protocols"), by: v.string() },
  handler: async (ctx, { id, by }) => {
    const who = by.trim(); if (!who) throw new Error("Enter your name first.");
    await limiter.limit(ctx, "submit", { key: who.toLowerCase(), throws: true });
    const p = await ctx.db.get(id);
    if (!p) throw new Error("That protocol was deleted.");
    const tasks = toTasks(p.rows);
    const errors = check(tasks, await ctx.db.query("tubes").collect(), await ctx.db.query("wells").collect());
    const runId = await ctx.db.insert("runs", {
      request: `Protocol “${p.name}” v${p.version}`, requestedBy: who, executor: "sim", createdAt: Date.now(),
      planner: "protocol", plannerNote: describe(tasks), tasks, moves: expand(tasks), errors, protocolId: id, via: "protocol",
      status: errors.length ? "rejected" : "awaiting_approval",
    });
    await log(ctx, "checker", errors.length ? "refused" : "passed", errors.length ? errors.join(" ") : `Protocol "${p.name}" v${p.version}: ${expand(tasks).length} moves. Waiting for approval.`, runId);
    return runId;
  },
});

function toTasks(rows: { tube: Task["tube"]; well: number; ml: number }[]): Task[] {
  const order = ["A", "B", "C", "D"] as const;
  return order.flatMap((tube) => {
    const pours = rows.filter((r) => r.tube === tube).map((r) => ({ well: r.well, ml: r.ml }));
    return pours.length ? [{ tube, pours }] : [];
  });
}
const describe = (tasks: Task[]) => tasks.map((t) => `${t.tube} → ${t.pours.map((p) => `well ${p.well} ${p.ml} mL`).join(", ")}`).join("; ");


