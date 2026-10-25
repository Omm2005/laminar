import { RateLimiter, MINUTE } from "@convex-dev/rate-limiter";
import { components } from "./_generated/api";

/** Rate limits, so a stuck client, voice loop or the Mana integration can't flood the bench or the Claude API. */
export const limiter = new RateLimiter(components.rateLimiter, {
  submit: { kind: "token bucket", rate: 12, period: MINUTE, capacity: 4 },      // per operator name
  mana: { kind: "token bucket", rate: 20, period: MINUTE, capacity: 8 },        // Mana console, global
  chat: { kind: "token bucket", rate: 20, period: MINUTE, capacity: 6 },        // assistant messages per name
  voice: { kind: "token bucket", rate: 30, period: MINUTE, capacity: 10 },      // speech requests per name
});
