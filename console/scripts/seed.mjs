// Seed the Convex `flowTable` singleton from ../pour/data/pour.yaml.
// Usage:  node scripts/seed.mjs
// Reads CONVEX_URL from console/.env.local, or you can set it in the environment.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import YAML from "yaml";
import { ConvexHttpClient } from "convex/browser";
import { api } from "../convex/_generated/api.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const POUR_YAML = path.resolve(ROOT, "..", "pour", "data", "pour.yaml");
const ENV_FILE = path.resolve(ROOT, ".env.local");

function loadEnvFile(p) {
  if (!fs.existsSync(p)) return;
  for (const line of fs.readFileSync(p, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (!m) continue;
    const v = m[2].replace(/^["']|["']$/g, "");
    process.env[m[1]] ??= v;
  }
}
loadEnvFile(ENV_FILE);

const url = process.env.CONVEX_URL || process.env.VITE_CONVEX_URL;
if (!url) {
  console.error("CONVEX_URL (or VITE_CONVEX_URL) not set — run `npx convex dev` first.");
  process.exit(1);
}

const yaml = YAML.parse(fs.readFileSync(POUR_YAML, "utf8"));
const onsetDeg = Number(yaml.onset_deg);
const pourDeg = Number(yaml.pour_deg);
const flow = yaml.flow_table_ml_per_s || {};
const rates = Object.entries(flow)
  .map(([deg, mlPerS]) => ({ deg: Number(deg), mlPerS: Number(mlPerS) }))
  .sort((a, b) => a.deg - b.deg);

if (!Number.isFinite(onsetDeg) || !Number.isFinite(pourDeg) || rates.length === 0) {
  console.error("pour.yaml is missing onset_deg / pour_deg / flow_table_ml_per_s");
  process.exit(1);
}

console.log(`seeding flowTable @ ${url}`);
console.log(`  onsetDeg=${onsetDeg}  pourDeg=${pourDeg}  rates=${JSON.stringify(rates)}`);

const client = new ConvexHttpClient(url);
await client.mutation(api.flowTable.update, {
  onsetDeg, pourDeg, rates, updatedBy: "seed",
});
console.log("done.");
