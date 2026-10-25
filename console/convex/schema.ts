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
  v.literal("estop"), v.literal("rejected"),
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
          label: v.optional(v.string()),        // what the dashboard shows ("Pour 1.0 mL into Sample 2")
          placeholder: v.optional(v.boolean()), // a step the robot can't do for real yet
        }),
      ),
    ),
    // agent plans: every pour, in dashboard terms (vial slot, container number); the robot gets ids only
    pours: v.optional(v.array(v.object({ vial: v.number(), container: v.number(), ml: v.number() }))),
    threadId: v.optional(v.string()),
    approvedVia: v.optional(v.string()),        // "button" | "voice" | "chat"
    report: v.optional(v.any()),                // the executor's run report
    summary: v.optional(v.string()),            // the agent's plain-words explanation afterwards
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
    joints: v.array(v.number()),              // MEASURED J1..J6 in degrees (dashboard + 3D model)
    commanded: v.optional(v.array(v.number())), // target J1..J6 the arm_server is driving to
    gripperDeg: v.optional(v.number()),       // gripper angle in degrees from zero (drives 3D jaws)
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
    online: v.optional(v.boolean()),
  }).index("by_name", ["name"]),

  scale: defineTable({
    t: v.number(),
    grams: v.number(),
    jobId: v.optional(v.id("jobs")),
  })
    .index("by_t", ["t"])
    .index("by_jobId_t", ["jobId", "t"]),

  /** Vials sitting on the bench. slot 1..N, letter is the display label. */
  vials: defineTable({
    slot: v.number(),
    letter: v.string(),
    contents: v.string(),
    volumeMl: v.number(),
    updatedAt: v.number(),
  }).index("by_slot", ["slot"]),

  /** 12-channel reservoir, one row per channel. targetMl is "what the technician wants it to end up at". */
  channels: defineTable({
    number: v.number(),
    contents: v.string(),
    currentMl: v.number(),
    targetMl: v.number(),
    capacityMl: v.number(),
    updatedAt: v.number(),
  }).index("by_number", ["number"]),

  /** Chat thread for the WORK tab. One thread for v1. */
  messages: defineTable({
    threadId: v.string(),
    role: v.union(v.literal("user"), v.literal("assistant"), v.literal("tool")),
    text: v.string(),
    tool: v.optional(v.object({
      name: v.string(), // "propose_plan" | "ask_clarification" | "answer" | "set_constraint"
      args: v.any(),
    })),
    createdAt: v.number(),
  }).index("by_thread_t", ["threadId", "createdAt"]),

  /** Singleton: what the bench computer (bridge/run_plan.py) sees, reported every second. Absent or old = the
      bench computer is not connected. */
  benchStatus: defineTable({
    t: v.number(),
    simulated: v.boolean(),
    arm: v.object({ connected: v.boolean(), detail: v.string() }),
    camera: v.object({ connected: v.boolean(), detail: v.string() }),
    model: v.object({ loaded: v.boolean(), detail: v.string() }),
    calibration: v.object({ done: v.boolean(), detail: v.string() }),
    readHz: v.optional(v.number()),
    // what the camera + model read in each calibrated box (container n = box n, left to right)
    containers: v.array(v.object({ number: v.number(), box: v.string(), ml: v.number(), conf: v.number(),
                                   capacityMl: v.number(), reachable: v.boolean() })),
    notReady: v.array(v.string()),
    grabs: v.optional(v.array(v.string())),     // vials whose grab is recorded (robotics/paths); others are placeholders
    frameId: v.optional(v.id("_storage")),     // camera picture with the boxes and levels drawn on
    rawFrameId: v.optional(v.id("_storage")),  // the same picture, clean: what calibration is drawn on
    calibFrameId: v.optional(v.id("_storage")), // the picture at calibration, boxes numbered 1..n (Claude looks at it)
    frameW: v.optional(v.number()),            // camera resolution, px (box coordinates are in these)
    frameH: v.optional(v.number()),
    camIndex: v.optional(v.string()),
    streamPort: v.optional(v.number()),        // live MJPEG video served by the bench computer itself
    boxes: v.optional(v.array(v.array(v.number()))),   // current calibration [x, y, w, h], left to right
    busy: v.optional(v.string()),              // "running a plan"
  }),

  /** Requests from the dashboard to the bench computer, e.g. { kind: "calibrate" }. */
  benchCommands: defineTable({
    kind: v.string(),                     // "set_boxes" {boxes} | "set_camera" {index}
    args: v.optional(v.any()),
    status: v.string(),                   // pending | running | done | failed
    message: v.optional(v.string()),
    createdAt: v.number(),
  }).index("by_status", ["status"]),

  /** Technician-tunable key/value settings (e.g. tolerance_ml). */
  settings: defineTable({
    key: v.string(),
    value: v.any(),
    updatedAt: v.number(),
  }).index("by_key", ["key"]),
});
