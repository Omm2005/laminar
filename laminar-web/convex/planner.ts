"use node";
/**
 * Claude plans the run before and explains it after. It never sends motor commands:
 * its plan goes through the checker in runs.applyPlan, and a human approves it.
 * Without ANTHROPIC_API_KEY the deterministic rule planner in lib.ts is used.
 */
import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { z } from "zod";
import { v } from "convex/values";
import { internalAction } from "./_generated/server";
import { internal } from "./_generated/api";
import { MAX_A_ALIQUOTS, MAX_POUR_ML, MIN_POUR_ML, WELL_CAPACITY_ML, rulePlan } from "./lib";

const MODEL = "claude-opus-5-5";

const SYSTEM = `You plan liquid-handling runs for Laminar, a single robot arm on a fixed lab bench.
Bench: tubes A–D in a rack. A is the blood sample (red), B/C/D are reagents 1–3 (blue, yellow, green).
Wells 1–4 sit on one tray; well 4 is the control and only ever gets sample A.
The standard assay: A into wells 1, 2, 3, 4 at 1.0 mL each; then B into well 1, C into well 2, D into well 3 at 0.5 mL each.
Limits: one pour is ${MIN_POUR_ML}–${MAX_POUR_ML} mL; a well holds ${WELL_CAPACITY_ML} mL; tube A gives at most ${MAX_A_ALIQUOTS} aliquots per fill; sample A goes in before any reagent.
Return tasks: each task is one tube picked up once with its list of pours. Put tube A first.
If the request is ambiguous (no volume, unclear tube or well), return a short clarifying question instead of tasks; never guess volumes.
"note" is one plain sentence saying what you understood, for the technician to hear read back.`;

const Plan = z.object({
  note: z.string(),
  question: z.string().nullable(),
  tasks: z.array(z.object({
    tube: z.enum(["A", "B", "C", "D"]),
    pours: z.array(z.object({ well: z.number().int(), ml: z.number() })),
  })).nullable(),
});

export const plan = internalAction({
  args: { id: v.id("runs") },
  handler: async (ctx, { id }) => {
    const run = await ctx.runQuery(internal.runs.getInternal, { id });
    if (!run) return;
    // Structured requests (one row of a plan built in the Mana console) are parsed exactly; Claude is for free text.
    if (!process.env.ANTHROPIC_API_KEY || run.structured) {
      const r = rulePlan(run.request);
      await ctx.runMutation(internal.runs.applyPlan, { id, planner: "rules", note: r.note, tasks: r.tasks, question: r.question });
      return;
    }
    try {
      const client = new Anthropic();
      const res = await client.messages.parse({
        model: MODEL,
        max_tokens: 4000,
        output_config: { effort: "low", format: zodOutputFormat(Plan) },
        system: SYSTEM,
        messages: [{ role: "user", content: run.request }],
      });
      const out = res.parsed_output;
      if (res.stop_reason === "refusal" || !out) throw new Error(`planner returned no plan (${res.stop_reason})`);
      await ctx.runMutation(internal.runs.applyPlan, {
        id, planner: MODEL, note: out.note,
        tasks: out.tasks ?? undefined, question: out.question ?? undefined,
      });
    } catch (e) {
      // The arm never depends on the model: fall back to the rule planner and say so.
      const r = rulePlan(run.request);
      await ctx.runMutation(internal.runs.applyPlan, {
        id, planner: "rules", note: `${r.note} (Claude unavailable: ${(e as Error).message.slice(0, 80)})`, tasks: r.tasks, question: r.question,
      });
    }
  },
});

/** After a run: Claude explains the measured readout. The readout itself is kept unchanged. */
export const reflect = internalAction({
  args: { id: v.id("runs") },
  handler: async (ctx, { id }) => {
    if (!process.env.ANTHROPIC_API_KEY) return;
    const detail = await ctx.runQuery(internal.runs.getInternal, { id });
    if (!detail?.result) return;
    try {
      const client = new Anthropic();
      const res = await client.messages.create({
        model: MODEL,
        max_tokens: 2000,
        output_config: { effort: "low" },
        system: "You write the end-of-run note for a lab technician. Three sentences at most, plain words. Use only numbers given to you. If a pour was off by more than 10%, say which and give one likely physical cause (tube running low, drip, tilt onset changed).",
        messages: [{ role: "user", content: `Request: ${detail.request}\nMoves: ${detail.moves?.join(", ")}\nMeasured readout: ${detail.result}` }],
      });
      const text = res.content.flatMap((b) => (b.type === "text" ? [b.text] : [])).join(" ").trim();
      if (text) await ctx.runMutation(internal.runs.setSummary, { id, summary: text });
    } catch (e) {
      // Thrown so the workflow retries with backoff; the measured readout is already saved.
      if (e instanceof Anthropic.APIError && e.status !== undefined && e.status < 500 && e.status !== 429) return;
      throw e;
    }
  },
});
