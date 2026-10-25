import { useMemo } from "react";
import { useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";
import Arm3D, { type ChannelFill } from "./Arm3D";

const WELL_CHANNEL: Record<number, number> = { 1: 1, 2: 4, 3: 7, 4: 10 };
const DYE_NAME: Record<string, string> = { A: "red", B: "blue", C: "yellow", D: "green" };

/** The reBot in 3D, driven by the joint samples in Convex: simulated moves now, measured joints once the bench streams. */
export default function LiveArm({ height = 380, preset = "wide" }: { height?: number; preset?: "front" | "top" | "shoulder" | "wide" }) {
  const samples = useQuery(api.robot.recent);
  const bench = useQuery(api.bench.state);
  const now = Date.now();
  const latest = samples?.filter((s) => s.t <= now).at(-1) ?? samples?.[0];
  const move = latest?.move ?? "";
  const run = bench?.running;
  const status = !run ? "idle" : move.startsWith("pour") ? "pouring" : "running";
  // Which tube is in the gripper right now: the last grab without a release after it.
  const holding = useMemo(() => {
    if (!run?.moves) return null;
    const done = run.moves.slice(0, run.step ?? 0);
    const last = [...done].reverse().find((m) => m.startsWith("grab_") || m.startsWith("release_"));
    return last?.startsWith("grab_") ? last.slice(5) : null;
  }, [run?.moves, run?.step]);
  const channels: ChannelFill[] = (bench?.wells ?? []).map((w) => ({
    number: WELL_CHANNEL[w.number], currentMl: w.volumeMl, capacityMl: w.capacityMl,
    contents: w.contents.map((c) => DYE_NAME[c.tube]).join(" "),
  }));
  const pourWell = move.match(/^pour\(well(\d)/)?.[1];
  return (
    <div style={{ height, borderRadius: 6, overflow: "hidden", position: "relative", background: "#0f1618" }}>
      <Arm3D
        samples={(samples ?? []).map((s) => ({ t: s.t, joints: s.joints }))}
        status={status}
        gripperDeg={latest?.gripperDeg ?? 70}
        pourChannel={pourWell ? WELL_CHANNEL[+pourWell] : null}
        cameraPreset={preset}
        vision={{ live: true, channels }}
        vialInGripper={!!holding}
        vialColor={holding ? DYE_NAME[holding] : undefined}
      />
      <span className="arm-tag mono">{run ? `▶ ${move || "starting"}` : "arm at home"} · {latest?.source === "bench" ? "measured joints" : "simulated joints"}</span>
    </div>
  );
}
