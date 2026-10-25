// Seed the bench with 2 vials and 4 channels. Idempotent.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ConvexHttpClient } from "convex/browser";
import { api } from "../convex/_generated/api.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ENV_FILE = path.resolve(__dirname, "..", ".env.local");

function loadEnv(p) {
  if (!fs.existsSync(p)) return;
  for (const line of fs.readFileSync(p, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (!m) continue;
    process.env[m[1]] ??= m[2].replace(/^["']|["']$/g, "");
  }
}
loadEnv(ENV_FILE);
const url = process.env.CONVEX_URL || process.env.VITE_CONVEX_URL;
if (!url) { console.error("CONVEX_URL not set"); process.exit(1); }
const client = new ConvexHttpClient(url);

const vials = [
  { slot: 1, letter: "A", contents: "Red dye (water)",   volumeMl: 10 },
  { slot: 2, letter: "B", contents: "Blue dye (water)",  volumeMl: 10 },
];
const channels = Array.from({ length: 4 }, (_, i) => ({
  number: i + 1,
  contents: i === 0 ? "sample 1" : i === 1 ? "sample 2" : i === 2 ? "sample 3" : "sample 4",
  currentMl: 0,
  targetMl: 0,
  capacityMl: 10,
}));

console.log(`seeding bench @ ${url}`);
for (const v of vials) {
  await client.mutation(api.vials.upsert, v);
  console.log(`  vial ${v.letter} (${v.contents})`);
}
for (const c of channels) {
  await client.mutation(api.channels.upsert, c);
  console.log(`  channel ${c.number} (${c.contents})`);
}
await client.mutation(api.settings.set, { key: "tolerance_ml", value: 0.3 });
console.log("  setting tolerance_ml = 0.3");
console.log("done.");
