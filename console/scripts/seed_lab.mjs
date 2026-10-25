// Seed the lab bench the agent works on: 4 vials (A-D, the liquids in robotics/layout.yaml) and the 4 containers
// the arm can reach (channels 1-4 = well_1..well_4 = side-camera boxes A-D). Names are for people only; the robot
// gets ids. Idempotent:  bun scripts/seed_lab.mjs
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ConvexHttpClient } from "convex/browser";
import { api } from "../convex/_generated/api.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
for (const line of fs.readFileSync(path.resolve(__dirname, "..", ".env.local"), "utf8").split(/\r?\n/)) {
  const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
  if (m) process.env[m[1]] ??= m[2].replace(/^["']|["']$/g, "");
}
const client = new ConvexHttpClient(process.env.CONVEX_URL || process.env.VITE_CONVEX_URL);

const vials = [
  { slot: 1, letter: "A", contents: "Blue dye", volumeMl: 8 },
  { slot: 2, letter: "B", contents: "Red dye", volumeMl: 8 },
  { slot: 3, letter: "C", contents: "Green dye", volumeMl: 8 },
  { slot: 4, letter: "D", contents: "Yellow dye", volumeMl: 8 },
];
const containers = ["Sample 1", "Sample 2", "Sample 3", "Sample 4"];

for (const v of vials) await client.mutation(api.vials.upsert, v);
for (const [i, name] of containers.entries()) {
  await client.mutation(api.channels.upsert, { number: i + 1, contents: name, currentMl: 0, targetMl: 0, capacityMl: 4.5 });
}
const tol = await client.query(api.settings.get, { key: "tolerance_ml" });
if (tol == null) await client.mutation(api.settings.set, { key: "tolerance_ml", value: 0.2 });
console.log(`seeded ${vials.length} vials and ${containers.length} containers`);
