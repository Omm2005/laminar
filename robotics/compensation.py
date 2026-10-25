"""Torque feedforward fitted from Tiger data: gravity (dynamics.py, scaled per joint) + Coulomb friction.

arm_server.py --comp loads compensation.json and adds feedforward(...) to every joint's MIT torque, so the
position loop only corrects what the model misses (less sag on J2, no stick-slip on J1/J5/J6). Without
--comp nothing changes. The file is written by `fit`, from the arm's own measurements in Tiger:

    gravity_scale  {joint: k}   measured holding torque ~= k * dynamics.gravity_torque(q); only joints where
                                the model explains the data (r >= MIN_R) get one, others keep k = 0
    friction_nm    {joint: Nm}  half the torque difference between moving up and moving down (Coulomb)

    python compensation.py fit [--run RUN_ID]   # from that run's 100 Hz arm_trace (default: arm_samples)
    python compensation.py show
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILE = HERE / "compensation.json"
GRAVITY_PERIOD_S = 0.05    # dynamics.gravity_torque takes ~1 ms: refresh at 20 Hz, gravity changes slowly
FRICTION_VEL = 0.02        # rad/s where the friction term reaches ~76 % (tanh), same as arm_server
FRICTION_SHARE = 0.8       # push with this share of the fitted friction, never all of it (no self-driving)
MAX_FF_NM = {1: 2.0, 2: 10.0, 3: 10.0, 4: 3.0, 5: 1.0, 6: 1.0}   # clamp per joint, whatever the file says
MIN_R = 0.95               # gravity model must explain the data this well to be used on a joint
MOVE_DEG_S, HOLD_DEG_S = 2.0, 0.2


class Compensation:
    def __init__(self, gravity_scale: dict[int, float], friction_nm: dict[int, float], clock=None):
        import time
        self.gravity_scale = {int(k): float(v) for k, v in gravity_scale.items() if float(v)}
        self.friction_nm = {int(k): float(v) for k, v in friction_nm.items()}
        self.clock = clock or time.perf_counter
        self._g: dict[int, float] = {}
        self._g_t = -1e9

    @classmethod
    def load(cls, path: str | Path = FILE) -> "Compensation":
        d = json.loads(Path(path).read_text())
        return cls(d.get("gravity_scale", {}), d.get("friction_nm", {}))

    def feedforward(self, cmd_deg: dict[int, float], vel_ff_rad_s: dict[int, float]) -> dict[int, float]:
        """N.m per arm joint for the commanded pose (not the measured one: no feedback loop through the model)."""
        if self.gravity_scale and self.clock() - self._g_t >= GRAVITY_PERIOD_S:
            from dynamics import gravity_torque
            g = gravity_torque(cmd_deg)
            self._g = {m: k * g[m] for m, k in self.gravity_scale.items()}
            self._g_t = self.clock()
        out = {}
        for m in vel_ff_rad_s:
            tau = self._g.get(m, 0.0)
            tau += FRICTION_SHARE * self.friction_nm.get(m, 0.0) * math.tanh(vel_ff_rad_s[m] / FRICTION_VEL)
            lim = MAX_FF_NM.get(m, 1.0)
            out[m] = max(-lim, min(lim, tau))
        return out


def fit_samples(ts, q, tau, cmd) -> dict:
    """Arrays (n,), (n,6), (n,6), (n,6) -> {"gravity_scale", "friction_nm", "stats"}."""
    import numpy as np
    from dynamics import gravity_torque
    dt = np.maximum(np.diff(ts), 1e-3)[:, None]
    cv = np.vstack([np.zeros(6), np.diff(cmd, axis=0) / dt])
    hold = np.all(np.abs(cv) < HOLD_DEG_S, axis=1)
    out = {"gravity_scale": {}, "friction_nm": {}, "stats": {"samples": int(len(ts)), "holding": int(hold.sum())}}
    if hold.sum() >= 20:
        g = np.array([[gq[m] for m in range(1, 7)] for gq in
                      (gravity_torque({m: float(v) for m, v in enumerate(row, 1)}) for row in q[hold])])
        for j in range(6):
            p, y = g[:, j], tau[hold, j]
            if np.std(p) < 0.2:  # the poses barely change this joint's gravity load: can't tell
                continue
            k, b = np.polyfit(p, y, 1)
            r = float(np.corrcoef(p, y)[0, 1])
            out["stats"][f"J{j + 1}_gravity"] = {"k": round(float(k), 3), "offset": round(float(b), 3), "r": round(r, 3)}
            if r >= MIN_R and 0.5 < k < 1.5:
                out["gravity_scale"][j + 1] = round(float(k), 3)
    for j in range(6):
        up, down = tau[cv[:, j] > MOVE_DEG_S, j], tau[cv[:, j] < -MOVE_DEG_S, j]
        if len(up) >= 10 and len(down) >= 10:
            f = (float(np.median(up)) - float(np.median(down))) / 2
            out["stats"][f"J{j + 1}_friction"] = {"up": round(float(np.median(up)), 3),
                                                   "down": round(float(np.median(down)), 3), "n": len(up) + len(down)}
            if f > 0.05:
                out["friction_nm"][j + 1] = round(f, 3)
    return out


def _load_tiger(run_id: str | None):
    import numpy as np
    import psycopg
    conn = psycopg.connect(os.environ["TIGER_DATABASE_URL"])
    if run_id:
        sql = ("SELECT extract(epoch FROM a.ts), array_agg(a.pos_deg ORDER BY joint), array_agg(a.torque ORDER BY joint), "
               "array_agg(a.cmd_deg ORDER BY joint) FROM arm_trace a, arm_runs r WHERE r.run_id = %s "
               "AND a.ts BETWEEN r.started AND coalesce(r.ended, now()) GROUP BY a.ts HAVING count(*) = 6 ORDER BY 1")
        rows = conn.execute(sql, (run_id,)).fetchall()
    else:
        rows = conn.execute("SELECT extract(epoch FROM ts), array_agg(state_deg ORDER BY joint), "
                            "array_agg(torque ORDER BY joint), array_agg(cmd_deg ORDER BY joint) FROM arm_samples "
                            "GROUP BY ts HAVING count(*) = 6 ORDER BY 1").fetchall()
    rows = [r for r in rows if None not in r[2]]
    return tuple(np.array([r[i] for r in rows], dtype=float) for i in range(4))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fit", "show"])
    ap.add_argument("--run", help="fit: Tiger run id (its 100 Hz arm_trace); default: all arm_samples")
    ap.add_argument("--out", default=str(FILE))
    a = ap.parse_args()
    if a.command == "show":
        print(Path(a.out).read_text() if Path(a.out).exists() else "no compensation.json yet")
        return 0
    ts, q, tau, cmd = _load_tiger(a.run)
    if len(ts) < 50:
        print(f"only {len(ts)} samples: run something first")
        return 1
    fit = fit_samples(ts, q, tau, cmd)
    fit["source"] = a.run or "arm_samples"
    Path(a.out).write_text(json.dumps(fit, indent=2))
    print(json.dumps(fit, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
