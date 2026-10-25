import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();

/** Daily QC report at 08:00 America/Los_Angeles (checked hourly so DST shifts don't matter). */
crons.hourly("daily QC report", { minuteUTC: 0 }, internal.reports.tick, {});

/** Keep Tiger's continuous aggregates warm for the analytics tab. No-op without TIGER_DATABASE_URL. */
crons.interval("tiger health", { minutes: 15 }, internal.tiger.heartbeat, {});

/** Telemetry is for the live 3D arm; keep an hour. */
crons.interval("prune telemetry", { minutes: 10 }, internal.robot.prune, {});

crons.interval("prune voice cache", { hours: 6 }, internal.voiceStore.prune, {});

export default crons;
