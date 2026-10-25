import { query } from "./_generated/server";

/** Newest level sample overall, regardless of channel or job. Used by the Robot page to tell
 *  whether the vision pipeline is live right now (sample age < 5 s). */
export const latestAny = query({
  args: {},
  // _creationTime desc ≈ newest first for telemetry inserted in order; no custom index needed.
  handler: async (ctx) => await ctx.db.query("levels").order("desc").first(),
});
