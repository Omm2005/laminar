import { useQuery } from "convex/react";
import { api } from "../convex/_generated/api";

/** What the bench computer reports, shared by the Lab chat (can a plan run?) and the Bench page. */

export const ACTIVE = ["approved", "running", "pouring"];

export const STATUS_WORDS: Record<string, string> = {
  awaiting_approval: "Waiting for approval", approved: "Approved, waiting for the robot", running: "Running",
  pouring: "Pouring", done: "Done", failed: "Stopped", estop: "Stopped by STOP", stalled: "Stalled",
  rejected: "Rejected",
};


export type Station = NonNullable<ReturnType<typeof useStation>>;
export function useStation() { return useQuery(api.station.status, {}); }
/** Why the robot can't run a plan right now, in words (null = ready). Mirrors station.readiness on the server. */
export function notReadyReason(st: Station | undefined): string | null {
  if (st === undefined) return "checking the bench…";
  if (!st.connected) return "The bench computer is not connected yet.";
  if (!("arm" in st)) return "The bench computer is not connected yet.";
  if (!st.arm.connected) return "The robot arm is not connected yet.";
  if (!st.camera.connected) return "The side camera is not connected yet.";
  if (!st.calibration.done) return "Calibration left: calibrate the side camera first.";
  if (st.busy) return `The bench is busy (${st.busy}).`;
  return null;
}


export function errText(e: unknown) {
  return (e as Error).message.replace(/^.*Uncaught Error: /, "").split("\n")[0];
}

