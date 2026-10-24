import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();

/** Refit flowTable.rates from the last 20 pours. */
crons.interval(
  "refit flowTable from recent pours",
  { minutes: 10 },
  internal.flowTable.refitInternal,
  {},
);

export default crons;
