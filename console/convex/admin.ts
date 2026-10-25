import { internalMutation } from "./_generated/server";

const TABLES = ["jobs", "steps", "telemetry", "levels", "pours", "events", "flowTable", "benchNodes", "scale",
                "vials", "channels", "messages", "settings", "benchStatus", "benchCommands"] as const;

/** Delete every row (and stored image) on THIS deployment. Run by hand only:
 *    bunx convex run admin:wipe */
export const wipe = internalMutation({
  args: {},
  handler: async (ctx) => {
    const counts: Record<string, number> = {};
    for (const t of TABLES) {
      let n = 0;
      for (const row of await ctx.db.query(t as never).collect()) {
        await ctx.db.delete((row as { _id: never })._id);
        n++;
      }
      counts[t] = n;
    }
    for (const f of await ctx.db.system.query("_storage").collect()) await ctx.storage.delete(f._id);
    return counts;
  },
});
