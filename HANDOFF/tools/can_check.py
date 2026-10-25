"""CAN health at rest: hold every motor where it is at 100 Hz for 4 s and count feedback gaps per motor."""
import sys, time, math
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "robotics"))
from robstride_can import RobStrideBus, JOINTS, P_MECH_POS, P_RUN_MODE, fine_timer
from leader_arm import load_follower_zero
fine_timer()
zero = load_follower_zero()
b = RobStrideBus()
try:
    b.drain(0.1)
    pos = {}
    for m in JOINTS:
        v = b.read_param_f32(m, P_MECH_POS, timeout=0.2)
        pos[m] = v
    print("positions read:", {m: (None if v is None else round(math.degrees(v) - zero[m], 1)) for m, v in pos.items()})
    if any(v is None for v in pos.values()):
        raise SystemExit("some motors did not answer")
    rel = {m: math.degrees(pos[m]) - zero[m] for m in JOINTS}
    rel = {m: d - 360 * round(d / 360) for m, d in rel.items()}
    if any(abs(rel[m]) > 15 for m in range(1, 6)):
        raise SystemExit(f"not at rest {rel}: not enabling")
    for m in JOINTS:
        b.disable(m); b.write_param_u8(m, P_RUN_MODE, 0)
    time.sleep(0.05)
    for m in JOINTS:
        b.enable(m)
    last = {m: time.time() for m in JOINTS}; gap = {m: 0.0 for m in JOINTS}; n = {m: 0 for m in JOINTS}
    t0 = time.time(); nxt = t0
    while time.time() - t0 < 4.0:
        for m in JOINTS:
            kp, kd = JOINTS[m][1], JOINTS[m][2]
            b.mit(m, pos[m], kp if m != 7 else 0.0, kd if m != 7 else 1.0)
        now = time.time()
        for m in JOINTS:
            st = b.state.get(m)
            if st:
                if st["t"] > last[m]:
                    n[m] += 1
                gap[m] = max(gap[m], now - st["t"])
                last[m] = st["t"]
        nxt += 0.01
        time.sleep(max(0, nxt - time.time()))
    print("feedback updates in 4 s:", n)
    print("worst feedback gap (s):", {m: round(g, 3) for m, g in gap.items()}, "rx_errors", b.rx_errors)
finally:
    for m in JOINTS:
        b.disable(m)
    time.sleep(0.05)
    b.close()
