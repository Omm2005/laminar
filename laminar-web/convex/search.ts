import { v } from "convex/values";
import { query } from "./_generated/server";

/** Full-text search over the audit log ("swap", "refused", a person's name, a well...). */
export const audit = query({
  args: { text: v.string(), actor: v.optional(v.string()) },
  handler: async (ctx, { text, actor }) => {
    if (!text.trim()) return [];
    return ctx.db.query("audit").withSearchIndex("search_detail", (q) => (actor ? q.search("detail", text).eq("actor", actor) : q.search("detail", text))).take(50);
  },
});
