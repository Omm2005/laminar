"use node";

import { internalAction } from "./_generated/server";
import { v } from "convex/values";
import { internal } from "./_generated/api";
import Anthropic from "@anthropic-ai/sdk";

// Fixed step order from pour/run_task.py: grasp, lift, traverse, lower, adaptive_pour,
// untilt, lift, return_vial, home.
const STEP_ORDER = [
  "grasp", "lift", "traverse", "lower", "adaptive_pour",
  "untilt", "lift", "return_vial", "home",
] as const;

const MAX_CHANNEL = 12;
const MAX_VIAL = 4; // sentinels; pour/data/poses.yaml today only defines vial_1_*
const CHANNEL_CAPACITY_ML = 10.0; // matches pour/data/channels.yaml default

type Plan = { steps: { name: string; args?: Record<string, unknown> }[]; targetMl: number; channel: number; vial: number };

function validate(plan: Plan): string | null {
  if (!plan.steps || plan.steps.length !== STEP_ORDER.length) {
    return `plan must have ${STEP_ORDER.length} steps, got ${plan.steps?.length}`;
  }
  for (let i = 0; i < STEP_ORDER.length; i++) {
    if (plan.steps[i].name !== STEP_ORDER[i]) {
      return `step ${i + 1} must be "${STEP_ORDER[i]}", got "${plan.steps[i].name}"`;
    }
  }
  if (!Number.isInteger(plan.channel) || plan.channel < 1 || plan.channel > MAX_CHANNEL) {
    return `channel must be an integer 1..${MAX_CHANNEL}, got ${plan.channel}`;
  }
  if (!Number.isInteger(plan.vial) || plan.vial < 1 || plan.vial > MAX_VIAL) {
    return `vial must be an integer 1..${MAX_VIAL}, got ${plan.vial}`;
  }
  if (!Number.isFinite(plan.targetMl) || plan.targetMl < 0.5 || plan.targetMl > CHANNEL_CAPACITY_ML) {
    return `targetMl must be 0.5..${CHANNEL_CAPACITY_ML}, got ${plan.targetMl}`;
  }
  return null;
}

const SYSTEM_PROMPT = `You are the planner for a lab pouring robot. The robot has a fixed primitive sequence it always runs:
  1. grasp vial N        pick up a vial by number
  2. lift                lift straight up a fixed distance
  3. traverse channel M  move horizontally over reservoir channel M
  4. lower               lower straight down a fixed distance
  5. adaptive_pour       closed-loop pour of target_ml into channel M (vision-feedback)
  6. untilt              rotate wrist back to upright
  7. lift                clear the channel
  8. return_vial N       traverse back over vial N
  9. home                return to the home pose

Interpret the user's one-sentence pour request and emit a JSON plan.
Reply with ONLY a JSON object matching this schema (no prose, no fencing):
{
  "targetMl":  <number of mL to pour>,
  "channel":   <integer 1..12, the reservoir trough>,
  "vial":      <integer 1..4, the source vial>,
  "steps": [
    {"name": "grasp",         "args": {"vial":   <vial>}},
    {"name": "lift",          "args": {}},
    {"name": "traverse",      "args": {"channel": <channel>}},
    {"name": "lower",         "args": {}},
    {"name": "adaptive_pour", "args": {"targetMl": <targetMl>, "channel": <channel>}},
    {"name": "untilt",        "args": {}},
    {"name": "lift",          "args": {}},
    {"name": "return_vial",   "args": {"vial":   <vial>}},
    {"name": "home",          "args": {}}
  ]
}
Rules:
- Default vial to 1 if the user doesn't say.
- "half of channel M" means half the channel capacity (treat capacity as 10 mL unless told otherwise).
- "quarter / third / all" scale the same way.
- Numeric mL is literal.
- Never deviate from the 9-step order above.`;

export const plan = internalAction({
  args: { jobId: v.id("jobs") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const job = await ctx.runQuery(internal.jobs.getInternal, { jobId: args.jobId });
    if (!job) return null;

    const key = process.env.ANTHROPIC_API_KEY;
    if (!key) {
      await ctx.runMutation(internal.jobs.failInternal, {
        jobId: args.jobId,
        reason: "ANTHROPIC_API_KEY is not set in the Convex dashboard environment variables.",
      });
      return null;
    }

    let plan: Plan;
    try {
      const client = new Anthropic({ apiKey: key });
      const msg = await client.messages.create({
        model: "claude-sonnet-4-6",
        max_tokens: 1024,
        system: SYSTEM_PROMPT,
        messages: [{ role: "user", content: job.requestText }],
      });
      const text = msg.content
        .filter((b): b is Anthropic.TextBlock => b.type === "text")
        .map((b) => b.text)
        .join("")
        .trim();
      // Strip accidental code fences if the model slipped them in.
      const stripped = text.replace(/^```(?:json)?/i, "").replace(/```$/i, "").trim();
      plan = JSON.parse(stripped) as Plan;
    } catch (e) {
      await ctx.runMutation(internal.jobs.failInternal, {
        jobId: args.jobId,
        reason: `planner model call failed: ${(e as Error).message}`,
      });
      return null;
    }

    const err = validate(plan);
    if (err) {
      await ctx.runMutation(internal.jobs.failInternal, {
        jobId: args.jobId,
        reason: `validator rejected plan: ${err}`,
      });
      return null;
    }

    await ctx.runMutation(internal.jobs.applyPlanInternal, {
      jobId: args.jobId,
      targetMl: plan.targetMl,
      channel: plan.channel,
      vial: plan.vial,
      plan: plan.steps,
    });
    return null;
  },
});
