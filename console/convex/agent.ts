"use node";

import { action, internalAction } from "./_generated/server";
import { v } from "convex/values";
import { internal, api } from "./_generated/api";
import Anthropic from "@anthropic-ai/sdk";

/** Lab-tech-facing chat agent. One of four tool-use shapes per reply:
 *    propose_plan({steps: [{vial, channel, targetMl}, ...]})
 *    ask_clarification({question})
 *    answer({text})
 *    set_constraint({key, value})
 *  Falls back to a regex parser when ANTHROPIC_API_KEY isn't set so the WORK tab is
 *  usable in a demo without a key.
 */

const TOOLS: Anthropic.Tool[] = [
  {
    name: "propose_plan",
    description: "Propose a worklist of pours. Each step is one pour from one vial into one channel.",
    input_schema: {
      type: "object",
      properties: {
        steps: {
          type: "array",
          items: {
            type: "object",
            properties: {
              vial: { type: "integer", description: "vial slot number 1..4" },
              channel: { type: "integer", description: "reservoir channel number 1..12" },
              targetMl: { type: "number", description: "mL to pour into this channel" },
            },
            required: ["vial", "channel", "targetMl"],
          },
        },
      },
      required: ["steps"],
    },
  },
  {
    name: "ask_clarification",
    description: "Ask the technician a short clarifying question before planning.",
    input_schema: {
      type: "object",
      properties: { question: { type: "string" } },
      required: ["question"],
    },
  },
  {
    name: "answer",
    description: "Answer a question with free text (e.g. 'why did it stop' using events).",
    input_schema: {
      type: "object",
      properties: { text: { type: "string" } },
      required: ["text"],
    },
  },
  {
    name: "set_constraint",
    description: "Set a tunable setting (e.g. tolerance_ml).",
    input_schema: {
      type: "object",
      properties: {
        key: { type: "string" },
        value: { description: "number, string, or boolean" },
      },
      required: ["key", "value"],
    },
  },
];

const SYSTEM = `You are the lab technician's co-pilot for a bench-top pouring robot.
A single bench has vials (slots A..D) and a 12-channel reservoir. Your job is to turn free-form
requests into exact worklists the technician approves, or to answer questions about what happened.

Pick ONE tool per reply:
  propose_plan   - a concrete worklist. Each step is one pour from one vial into one channel.
  ask_clarification - if the request is missing which vial, channel, or volume.
  answer         - free text; use this for "why did it stop" and similar from the event log.
  set_constraint - update a technician-tunable setting (e.g. tolerance_ml).

Rules:
- vial must be 1..4; channel must be 1..12.
- targetMl per step must be between 0.5 and the channel capacity.
- "channels 1 to 4, 1.5 mL each" is a single propose_plan with 4 steps.
- If the user says "half" / "quarter", resolve against the channel capacity in the bench state.
- Never invent vials or channels not in the bench state.
- Reply with EXACTLY ONE tool call. No prose, no multiple tools.`;

const FRACS: Record<string, number> = { half: 0.5, quarter: 0.25, third: 1 / 3, all: 1 };

/** Deterministic parse used when ANTHROPIC_API_KEY is absent. */
function regexPlan(text: string, channels: { number: number; capacityMl: number }[]):
  { steps: { vial: number; channel: number; targetMl: number }[] } | string {
  const t = text.trim().toLowerCase();
  // "channels A to B, N mL each (from vial V)"
  let m = t.match(/channels?\s+(\d+)\s+to\s+(\d+),?\s*([\d.]+)\s*ml?\s*each(?:\s+from\s+vial\s+(\d+))?/);
  if (m) {
    const a = +m[1], b = +m[2], ml = +m[3], vial = +(m[4] ?? 1);
    const steps = [];
    for (let c = a; c <= b; c++) steps.push({ vial, channel: c, targetMl: ml });
    return { steps };
  }
  // "N ml into channel M [from vial V]"
  m = t.match(/pour?\s*([\d.]+)\s*ml?\s+into\s+channel\s+(\d+)(?:\s+from\s+vial\s+(\d+))?/);
  if (m) return { steps: [{ vial: +(m[3] ?? 1), channel: +m[2], targetMl: +m[1] }] };
  // "half/quarter of channel M [from vial V]"
  m = t.match(/pour?\s*(\w+)\s+of\s+channel\s+(\d+)(?:\s+from\s+vial\s+(\d+))?/);
  if (m) {
    const frac = FRACS[m[1]];
    const ch = +m[2];
    const cap = channels.find((c) => c.number === ch)?.capacityMl ?? 10;
    if (!frac) return `I don't know the fraction '${m[1]}'. Say half, quarter, third, or a number.`;
    return { steps: [{ vial: +(m[3] ?? 1), channel: ch, targetMl: cap * frac }] };
  }
  return "I couldn't parse that. Try `pour 2 ml into channel 3 from vial 1` or `channels 1 to 4, 1.5 ml each`.";
}

export const chat = internalAction({
  args: { threadId: v.string(), userMessage: v.string() },
  handler: async (ctx, args) => {
    // bench state as compact context
    const [vials, channels, flow, recentEvents, recentPours, history] = await Promise.all([
      ctx.runQuery(api.vials.list, {}),
      ctx.runQuery(api.channels.list, {}),
      ctx.runQuery(api.flowTable.get, {}),
      ctx.runQuery(api.events.recent, { limit: 10 }),
      ctx.runQuery(api.pours.recent, { limit: 10 }),
      ctx.runQuery(internal.messages.listInternal, { threadId: args.threadId, limit: 20 }),
    ]);
    const benchJson = JSON.stringify({ vials, channels, flow, recentEvents, recentPours }, null, 2);

    const key = process.env.ANTHROPIC_API_KEY;
    if (!key) {
      // offline fallback: regex parser
      const r = regexPlan(args.userMessage, channels);
      if (typeof r === "string") {
        await ctx.runMutation(internal.messages.appendInternal, {
          threadId: args.threadId, role: "assistant", text: r,
          tool: { name: "answer", args: { text: r } },
        });
      } else {
        const n = r.steps.length;
        const text = n === 1
          ? `Proposed: ${r.steps[0].targetMl.toFixed(1)} mL into channel ${r.steps[0].channel} from vial ${r.steps[0].vial}.`
          : `Proposed ${n}-step worklist. Approve to run.`;
        await ctx.runMutation(internal.messages.appendInternal, {
          threadId: args.threadId, role: "assistant", text,
          tool: { name: "propose_plan", args: r },
        });
      }
      return null;
    }

    const client = new Anthropic({ apiKey: key });
    try {
      const msg = await client.messages.create({
        model: "claude-sonnet-4-6",
        max_tokens: 1024,
        system: SYSTEM + "\n\nBENCH STATE (read-only):\n" + benchJson,
        tools: TOOLS,
        tool_choice: { type: "any" },
        messages: [
          ...history
            .filter((m) => m.role !== "tool")
            .map((m) => ({
              role: m.role === "user" ? ("user" as const) : ("assistant" as const),
              content: m.text,
            })),
          { role: "user", content: args.userMessage },
        ],
      });
      const toolUse = msg.content.find((b): b is Anthropic.ToolUseBlock => b.type === "tool_use");
      if (!toolUse) {
        const text = msg.content
          .filter((b): b is Anthropic.TextBlock => b.type === "text")
          .map((b) => b.text).join("");
        await ctx.runMutation(internal.messages.appendInternal, {
          threadId: args.threadId, role: "assistant",
          text: text || "(no response)",
          tool: { name: "answer", args: { text } },
        });
        return null;
      }
      const text = toolUse.name === "propose_plan"
        ? `Proposed ${(toolUse.input as { steps: unknown[] }).steps.length}-step worklist. Approve to run.`
        : toolUse.name === "ask_clarification"
        ? (toolUse.input as { question: string }).question
        : toolUse.name === "answer"
        ? (toolUse.input as { text: string }).text
        : `Setting ${(toolUse.input as { key: string }).key} = ${JSON.stringify((toolUse.input as { value: unknown }).value)}`;

      await ctx.runMutation(internal.messages.appendInternal, {
        threadId: args.threadId, role: "assistant", text,
        tool: { name: toolUse.name, args: toolUse.input },
      });

      // Side-effect: set_constraint writes to the settings table immediately.
      if (toolUse.name === "set_constraint") {
        const { key: k, value } = toolUse.input as { key: string; value: unknown };
        await ctx.runMutation(api.settings.set, { key: k, value });
      }
    } catch (e) {
      await ctx.runMutation(internal.messages.appendInternal, {
        threadId: args.threadId, role: "assistant",
        text: `(agent error: ${(e as Error).message})`,
        tool: { name: "answer", args: { text: String(e) } },
      });
    }
    return null;
  },
});

/** User approves a proposed_plan: creates one job per step (reusing the existing jobs flow). */
export const approvePlan = action({
  args: {
    steps: v.array(v.object({ vial: v.number(), channel: v.number(), targetMl: v.number() })),
    approvedBy: v.string(),
  },
  handler: async (ctx, args) => {
    for (const s of args.steps) {
      const letter = String.fromCharCode(64 + s.vial);
      const text = `pour ${s.targetMl} ml into channel ${s.channel} from vial ${s.vial}`;
      // Submit the request (planner runs); if the planner fails, we auto-approve the resulting job here.
      const jobId = await ctx.runMutation(api.requests.submit, {
        requestText: text, requestedBy: `agent:${args.approvedBy}:${letter}`,
      });
      // Wait a tick, then approve (planner usually finishes within ~100 ms with the regex fallback).
      await new Promise((r) => setTimeout(r, 400));
      try {
        await ctx.runMutation(api.jobs.approve, { jobId, approvedBy: args.approvedBy });
      } catch {
        /* planner might still be running; the technician can approve manually */
      }
    }
    return null;
  },
});
