/**
 * Arm poses for the simulated bench, so the 3D arm moves the way the real one would.
 * Joint angles are degrees J1..J6 in the follower frame used by robotics/arm_server.py.
 * Tube grab poses and the four pour-station poses are the team's taught values from robotics/spots.yaml
 * (vial_X_initial_position / vial_X_final_position). Lift, carry and tilt offsets are approximations:
 * the real bench computes them with IK, which is not ported here.
 */
type J = [number, number, number, number, number, number];

export const HOME: J = [0, 110, 40, 75, 0, 0];

export const SLOT: Record<string, J> = {
  A: [14.5, 99.2, 28.1, 72.4, -0.8, 2.0],
  B: [14.5, 92.3, 33.9, 51.6, -11.4, 0.5],
  C: [14.7, 95.685, 38.398, 50.2, -19.602, 1.799],
  D: [34.841, 104.918, 37.032, 70.504, 19.0, -11.9],
};

export const WELL: Record<number, J> = {
  1: [-65.5, 119.4, 45.8, 89.1, -63.7, 12.0],
  2: [-61.2, 122.7, 50.5, 89.0, -59.2, 10.3],
  3: [-58.3, 124.4, 54.0, 86.7, -56.9, 8.9],
  4: [-55.6, 127.0, 58.7, 85.9, -53.5, 10.7],
};

const lift = (p: J, cm: number): J => [p[0], p[1] - cm * 1.4, p[2] - cm * 0.9, p[3], p[4], p[5]];
const tilt = (p: J, deg: number): J => [p[0], p[1], p[2], p[3], p[4], p[5] + deg];

export const GRIPPER_OPEN = 70, GRIPPER_HOLD = 22;
export const POUR_TILT = 62;

/** Target pose, gripper opening and duration for one move, given where the arm is. */
export function poseFor(move: string, at: { spot: J }): { joints: J; gripper: number; ms: number } {
  let m: RegExpMatchArray | null;
  if ((m = move.match(/^grab_([ABCD])$/))) return { joints: SLOT[m[1]], gripper: GRIPPER_HOLD, ms: 900 };
  if ((m = move.match(/^release_([ABCD])$/))) return { joints: SLOT[m[1]], gripper: GRIPPER_OPEN, ms: 600 };
  if ((m = move.match(/^lift\((\d+)\)$/))) return { joints: lift(at.spot, parseInt(m[1])), gripper: GRIPPER_HOLD, ms: 600 };
  if ((m = move.match(/^lower\((\d+)\)$/))) return { joints: at.spot, gripper: GRIPPER_HOLD, ms: 600 };
  if ((m = move.match(/^travel_to\(well(\d)\)$/))) return { joints: lift(WELL[+m[1]], 6), gripper: GRIPPER_HOLD, ms: 900 };
  if ((m = move.match(/^travel_to\(slot_([ABCD])\)$/))) return { joints: lift(SLOT[m[1]], 10), gripper: GRIPPER_HOLD, ms: 900 };
  if ((m = move.match(/^pour\(well(\d)/))) return { joints: tilt(WELL[+m[1]], POUR_TILT), gripper: GRIPPER_HOLD, ms: 1600 };
  return { joints: at.spot, gripper: GRIPPER_HOLD, ms: 450 };
}

/** The spot a lower() returns to: the last taught pose the arm travelled toward. */
export function spotAfter(move: string, prev: J): J {
  let m: RegExpMatchArray | null;
  if ((m = move.match(/^grab_([ABCD])$/)) || (m = move.match(/^travel_to\(slot_([ABCD])\)$/))) return SLOT[m[1]];
  if ((m = move.match(/^travel_to\(well(\d)\)$/))) return WELL[+m[1]];
  return prev;
}
