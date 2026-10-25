/* eslint-disable */
/**
 * Generated `api` utility.
 *
 * THIS CODE IS AUTOMATICALLY GENERATED.
 *
 * To regenerate, run `npx convex dev`.
 * @module
 */

import type * as bench from "../bench.js";
import type * as crons from "../crons.js";
import type * as exec from "../exec.js";
import type * as http from "../http.js";
import type * as lib from "../lib.js";
import type * as limits from "../limits.js";
import type * as mana from "../mana.js";
import type * as planner from "../planner.js";
import type * as poses from "../poses.js";
import type * as protocols from "../protocols.js";
import type * as qc from "../qc.js";
import type * as reports from "../reports.js";
import type * as robot from "../robot.js";
import type * as runs from "../runs.js";
import type * as search from "../search.js";
import type * as site from "../site.js";
import type * as tiger from "../tiger.js";
import type * as voice from "../voice.js";
import type * as voiceStore from "../voiceStore.js";

import type {
  ApiFromModules,
  FilterApi,
  FunctionReference,
} from "convex/server";

declare const fullApi: ApiFromModules<{
  bench: typeof bench;
  crons: typeof crons;
  exec: typeof exec;
  http: typeof http;
  lib: typeof lib;
  limits: typeof limits;
  mana: typeof mana;
  planner: typeof planner;
  poses: typeof poses;
  protocols: typeof protocols;
  qc: typeof qc;
  reports: typeof reports;
  robot: typeof robot;
  runs: typeof runs;
  search: typeof search;
  site: typeof site;
  tiger: typeof tiger;
  voice: typeof voice;
  voiceStore: typeof voiceStore;
}>;

/**
 * A utility for referencing Convex functions in your app's public API.
 *
 * Usage:
 * ```js
 * const myFunctionReference = api.myModule.myFunction;
 * ```
 */
export declare const api: FilterApi<
  typeof fullApi,
  FunctionReference<any, "public">
>;

/**
 * A utility for referencing Convex functions in your app's internal API.
 *
 * Usage:
 * ```js
 * const myFunctionReference = internal.myModule.myFunction;
 * ```
 */
export declare const internal: FilterApi<
  typeof fullApi,
  FunctionReference<any, "internal">
>;

export declare const components: {
  workflow: import("@convex-dev/workflow/_generated/component.js").ComponentApi<"workflow">;
  rateLimiter: import("@convex-dev/rate-limiter/_generated/component.js").ComponentApi<"rateLimiter">;
};
