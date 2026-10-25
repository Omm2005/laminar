import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export const TUBE = v.union(v.literal("A"), v.literal("B"), v.literal("C"), v.literal("D"));

export const RUN_STATUS = v.union(
  v.literal("checking"),          // planner + checker working
  v.literal("rejected"),          // checker found errors; nothing moves
  v.literal("awaiting_approval"), // plan read back, waiting for a human
  v.literal("approved"),          // queued for the executor
  v.literal("running"),
  v.literal("done"),
  v.literal("aborted"),
);

/** One task = one tube picked up once, poured into one or more wells, put back. */
export const TASK = v.object({
  tube: TUBE,
  pours: v.array(v.object({ well: v.number(), ml: v.number() })),
});

export default defineSchema({
  runs: defineTable({
    request: v.string(),
    requestedBy: v.string(),
    status: RUN_STATUS,
    executor: v.union(v.literal("sim"), v.literal("bench")),
    planner: v.optional(v.string()),       // "claude-opus-5-5" | "rules" | "protocol"
    structured: v.optional(v.boolean()),
    via: v.optional(v.union(v.literal("web"), v.literal("voice"), v.literal("mana"), v.literal("protocol"))),   // machine-written request (Mana console): parsed by rules, not Claude
    plannerNote: v.optional(v.string()),   // one line from the planner: what it understood
    question: v.optional(v.string()),      // clarifying question instead of a plan
    tasks: v.optional(v.array(TASK)),
    moves: v.optional(v.array(v.string())), // expanded move list, e.g. "grab_A", "pour(well1, 1.0)"
    errors: v.optional(v.array(v.string())),
    approvedBy: v.optional(v.string()),
    step: v.optional(v.number()),          // index into moves while running
    abortReason: v.optional(v.string()),
    workflowId: v.optional(v.string()),     // the durable executor workflow (exec.ts)
    protocolId: v.optional(v.id("protocols")),
    result: v.optional(v.string()),        // measured readout written by the executor
    summary: v.optional(v.string()),       // Claude's note on the readout, if a key is set
    createdAt: v.number(),
    startedAt: v.optional(v.number()),
    finishedAt: v.optional(v.number()),
  })
    .index("by_createdAt", ["createdAt"])
    .index("by_status", ["status"]),

  pours: defineTable({
    runId: v.id("runs"),
    tube: TUBE,
    well: v.number(),
    targetMl: v.number(),
    pouredMl: v.number(),
    errorMl: v.number(),
    source: v.union(v.literal("sim"), v.literal("bench")),
    t: v.number(),
  })
    .index("by_t", ["t"])
    .index("by_runId", ["runId"])
    .index("by_source_t", ["source", "t"]),

  /** Physical bench: one row per tube slot and one per well. */
  tubes: defineTable({
    slot: v.number(),            // 1..4, left to right
    letter: TUBE,                // what is actually in the slot (the camera's reading)
    stands: v.string(),          // "Blood sample", "Reagent 1", ...
    dye: v.string(),             // css color for the demo liquid
    volumeMl: v.number(),
    fillLineMl: v.number(),
    updatedAt: v.number(),
  }).index("by_slot", ["slot"]),

  wells: defineTable({
    number: v.number(),          // 1..4; 4 is the control
    volumeMl: v.number(),
    capacityMl: v.number(),
    contents: v.array(v.object({ tube: TUBE, ml: v.number() })),
    updatedAt: v.number(),
  }).index("by_number", ["number"]),

  /** Append-only. No mutation updates or deletes these rows. */
  audit: defineTable({
    t: v.number(),
    actor: v.string(),           // person name, "planner", "checker", "executor", "cron"
    action: v.string(),
    detail: v.string(),
    runId: v.optional(v.id("runs")),
  })
    .index("by_t", ["t"])
    .searchIndex("search_detail", { searchField: "detail", filterFields: ["actor", "action"] }),

  /** Arm joints, J1..J6 in degrees. Written by the simulated executor or pushed by the bench bridge. */
  telemetry: defineTable({
    t: v.number(),
    joints: v.array(v.number()),
    gripperDeg: v.optional(v.number()),
    source: v.union(v.literal("sim"), v.literal("bench")),
    runId: v.optional(v.id("runs")),
    move: v.optional(v.string()),
  }).index("by_t", ["t"]).index("by_source_t", ["source", "t"]),

  /** Latest camera frames from the bench (side camera on the tray, wrist camera on the arm). Only the newest
   *  few are kept; the JPEGs live in file storage. */
  cameraFrames: defineTable({
    cam: v.union(v.literal("side"), v.literal("wrist")),
    storageId: v.id("_storage"),
    t: v.number(),
    bytes: v.number(),
  }).index("by_cam_t", ["cam", "t"]),

  /** Where the bench's live video is (stream.py behind a tunnel), refreshed every 10 s by publish.py. */
  streams: defineTable({ name: v.string(), url: v.string(), t: v.number() }).index("by_name", ["name"]),

  /** The live view: one row per camera holding its newest JPEG, overwritten in place. A live query on this row
   *  pushes the image bytes straight to every open page over the Convex websocket: no storage, no second download. */
  liveFrames: defineTable({
    cam: v.union(v.literal("side"), v.literal("wrist"), v.literal("test")),
    jpeg: v.bytes(),
    t: v.number(),         // server time the frame arrived
    captured: v.optional(v.number()), // bench time the camera owner wrote it; older arrivals are ignored
    seq: v.number(),       // increases with every frame, so the page can count fps
  }).index("by_cam", ["cam"]),

  /** Protocol Builder plans: a plate map of pours, saved and versioned before it becomes a run. */
  protocols: defineTable({
    name: v.string(),
    rows: v.array(v.object({ tube: TUBE, well: v.number(), ml: v.number() })),
    version: v.number(),
    createdBy: v.string(),
    updatedAt: v.number(),
    sourceFile: v.optional(v.id("_storage")),   // the CSV it was imported from, kept in file storage
    sourceName: v.optional(v.string()),
  }).index("by_updatedAt", ["updatedAt"]).searchIndex("search_name", { searchField: "name" }),

  /** Apps connected to this backend (the Mana Lab Console polls /mana/summary every 5 s). */
  integrations: defineTable({ name: v.string(), lastSeen: v.number(), polls: v.number() }).index("by_name", ["name"]),

  /** Synthesized speech, cached in file storage. Never holds microphone audio. */
  voiceCache: defineTable({ key: v.string(), storageId: v.id("_storage"), t: v.number() })
    .index("by_key", ["key"]).index("by_t", ["t"]),

  scores: defineTable({
    player: v.string(),
    targetMl: v.number(),
    pouredMl: v.number(),
    errorMl: v.number(),
    t: v.number(),
  }).index("by_t", ["t"]),

  reports: defineTable({
    date: v.string(),            // YYYY-MM-DD in America/Los_Angeles
    generatedAt: v.number(),
    trigger: v.union(v.literal("schedule"), v.literal("manual")),
    windowFrom: v.number(),
    windowTo: v.number(),
    runs: v.number(),
    done: v.number(),
    aborted: v.number(),
    abortReasons: v.array(v.string()),
    pours: v.number(),
    meanErrorMl: v.union(v.number(), v.null()),
    maeMl: v.union(v.number(), v.null()),
    inBandPct: v.union(v.number(), v.null()),
    perTube: v.array(v.object({ tube: TUBE, pours: v.number(), meanErrorMl: v.number(), maeMl: v.number() })),
    lowTubes: v.array(v.string()),
    recommendation: v.string(),
  }).index("by_date", ["date"]),
});
