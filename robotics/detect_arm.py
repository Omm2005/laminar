"""Passive scan of the reBot Arm B601-RS: pings motor IDs 1-7 and reads their positions. Never enables a motor."""
import math

from robstride_can import JOINTS, P_MECH_POS, P_VBUS, RobStrideBus

bus = RobStrideBus()
try:
    bus.drain(0.1)
    found = 0
    for mid, (model, *_rest) in JOINTS.items():
        if not bus.ping(mid, timeout=0.1):
            print(f"J{mid}: no response")
            continue
        found += 1
        pos = bus.read_param_f32(mid, P_MECH_POS)
        vbus = bus.read_param_f32(mid, P_VBUS)
        pos_s = f"{math.degrees(pos):8.2f} deg" if pos is not None else "?"
        vbus_s = f"{vbus:5.1f} V" if vbus is not None else "?"
        print(f"J{mid}: {model} online  pos={pos_s}  vbus={vbus_s}")
    print(f"{found}/{len(JOINTS)} motors found")
finally:
    bus.close()
