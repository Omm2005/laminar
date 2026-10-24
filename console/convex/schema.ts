import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

const JOB_STATUS = v.union(
  v.literal("queued"),
  v.literal("planning"),
  v.literal("awaiting_approval"),
  v.literal("approved"),
  v.literal("running"),
  v.literal("pouring"),
  v.literal("done"),
  v.literal("failed"),
  v.literal("stalled"),
  v.literal("estop"),
);

export default defineSchema({
  jobs: defineTable({
    requestText: v.string(),
    targetMl: v.number(),
    channel: v.number(),
    vial: v.number(),
    status: JOB_STATUS,
    currentStep: v.optional(v.string()),
    approvedBy: v.optional(v.string()),
    createdAt: v.number(),
    finishedAt: v.optional(v.number()),
    failReason: v.optional(v.string()),
    // planner output: the ordered primitive step names + their args
    plan: v.optional(
      v.array(
        v.object({
          name: v.string(),
          args: v.optional(v.any()),
        }),
      ),
    ),
  })
    .index("by_status", ["status"])
    .index("by_createdAt", ["createdAt"]),

  steps: defineTable({
    jobId: v.id("jobs"),
    name: v.string(),
    startedAt: v.number(),
    endedAt: v.optional(v.number()),
    ok: v.optional(v.boolean()),
    reason: v.optional(v.string()),
  }).index("by_jobId", ["jobId"]),

  telemetry: defineTable({
    jobId: v.optional(v.id("jobs")),
    t: v.number(),
    joints: v.array(v.number()), // length 6, J1..J6 in degrees
    gripperTorque: v.optional(v.number()),
    pourDeg: v.optional(v.number()),
  }).index("by_jobId_t", ["jobId", "t"])
    .index("by_t", ["t"]),

  levels: defineTable({
    jobId: v.optional(v.id("jobs")),
    channel: v.optional(v.number()),
    t: v.number(),
    ml: v.number(),
  }).index("by_jobId_t", ["jobId", "t"]),

  pours: defineTable({
    jobId: v.id("jobs"),
    targetMl: v.number(),
    pouredMl: v.number(),
    errorMl: v.number(),
    onsetDeg: v.number(),
    pourDeg: v.number(),
    measuredMlPerS: v.number(),
    snapshotBefore: v.optional(v.id("_storage")),
    snapshotAfter: v.optional(v.id("_storage")),
    createdAt: v.number(),
  })
    .index("by_jobId", ["jobId"])
    .index("by_createdAt", ["createdAt"]),

  events: defineTable({
    t: v.number(),
    kind: v.string(), // "estop" | "clear" | "info" | "warn" | "error" | ...
    source: v.string(), // "web" | "robot" | "watchdog" | "planner" | ...
    message: v.string(),
    jobId: v.optional(v.id("jobs")),
  })
    .index("by_t", ["t"])
    .index("by_kind_t", ["kind", "t"]),

  // Singleton: one row only. onsetDeg/pourDeg are the defaults; rates is the measured table.
  flowTable: defineTable({
    onsetDeg: v.number(),
    pourDeg: v.number(),
    rates: v.array(v.object({ deg: v.number(), mlPerS: v.number() })),
    updatedAt: v.number(),
    updatedBy: v.optional(v.string()), // "seed" | "cron-refit" | "manual:<name>"
  }),

  benchNodes: defineTable({
    name: v.string(),
    lastHeartbeat: v.number(),
    lastButton: v.optional(v.string()),
  }).index("by_name", ["name"]),
});
