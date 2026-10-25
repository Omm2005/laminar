"""What motor 6 (the wrist roll, the pour joint) did in recent runs, from Tiger Data.

    python -m laminar.wrist_report                 # the last 10 runs
    python -m laminar.wrist_report --runs 30
    python -m laminar.wrist_report --run <run_id>  # one run, with its J6 trace in 0.5 s steps

Per run: where J6 started, how far it was commanded and how far it really got (deg from the calibrated zero), time
spent within 3 deg of the +-90 deg limit, the worst tracking error and torque, the pour's max tilt, and any message
about the wrist's range ("no flow at max tilt", "room to the wrist limit"). Reads arm_trace (100 Hz) and falls back
to arm_samples (10 Hz).

The Tiger URL comes from TIGER_DATABASE_URL, or from a TIGER_DATABASE_URL=... line in ~/laminar/.env (git ignores it).
"""
from __future__ import annotations

import argparse
import os

from . import tiger
from .telemetry import REPO_ROOT

LIMIT_DEG = 90.0
NEAR_DEG = 3.0


def dsn() -> str | None:
    if os.environ.get(tiger.ENV_DSN):
        return os.environ[tiger.ENV_DSN]
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith(f"{tiger.ENV_DSN}="):
                return line.split("=", 1)[1].strip().strip("\"'") or None
    return None


J6_STATS = """
    SELECT count(*), min(cmd_deg), max(cmd_deg), min({pos}), max({pos}), max(abs(err_deg)), max(abs(torque)),
           sum(CASE WHEN abs({pos}) >= %(near)s THEN 1 ELSE 0 END)
    FROM {table} WHERE joint = 6 AND ts BETWEEN %(t0)s AND %(t1)s"""
J6_FIRST = "SELECT {pos} FROM {table} WHERE joint = 6 AND ts BETWEEN %(t0)s AND %(t1)s ORDER BY ts LIMIT 1"
POUR = "SELECT max(tilt), count(*) FROM pour_samples WHERE ts BETWEEN %(t0)s AND %(t1)s"
MESSAGES = """
    SELECT ts, event, coalesce(data->>'reason', data->>'result', data->>'message', '')
    FROM laminar_events WHERE ts BETWEEN %(t0)s AND %(t1)s
      AND (data::text ILIKE '%%max tilt%%' OR data::text ILIKE '%%wrist limit%%' OR data::text ILIKE '%%past its limit%%'
           OR event IN ('executor.abort', 'pour.result'))
    ORDER BY ts LIMIT 5"""
TRACE = """
    SELECT time_bucket('500 milliseconds', ts) AS t, avg(cmd_deg), avg(pos_deg), max(abs(torque)), max(op)
    FROM arm_trace WHERE joint = 6 AND ts BETWEEN %(t0)s AND %(t1)s GROUP BY 1 ORDER BY 1"""


def j6_window(cur, t0, t1) -> dict | None:
    """J6 stats between t0 and t1 from arm_trace (100 Hz), else arm_samples (10 Hz)."""
    for table, pos, hz in (("arm_trace", "pos_deg", 100.0), ("arm_samples", "state_deg", 10.0)):
        args = {"t0": t0, "t1": t1, "near": LIMIT_DEG - NEAR_DEG}
        cur.execute(J6_STATS.format(table=table, pos=pos), args)
        n, cmin, cmax, pmin, pmax, err, tau, near = cur.fetchone()
        if n:
            cur.execute(J6_FIRST.format(table=table, pos=pos), args)
            start = cur.fetchone()[0]
            return {"source": table, "samples": n, "start": start, "cmd": (cmin, cmax), "pos": (pmin, pmax),
                    "max_err": err, "max_torque": tau, "near_limit_s": near / hz}
    return None


def fmt(x, nd=1):
    return "—" if x is None else f"{x:+.{nd}f}"


def report_run(cur, run, trace: bool) -> None:
    run_id, name, started, ended, status = run
    t0, t1 = started, ended or started
    cur.execute("SELECT now()")
    if ended is None:
        t1 = cur.fetchone()[0]
    j = j6_window(cur, t0, t1)
    cur.execute(POUR, {"t0": t0, "t1": t1})
    pour_tilt, pour_n = cur.fetchone()
    print(f"\n{started:%Y-%m-%d %H:%M:%S}  {name or run_id}  [{status}]  ({(t1 - t0).total_seconds():.0f} s)")
    if not j:
        print("  no motor 6 data for this run (arm_server's trace / Tiger sampler not running?)")
    else:
        room_plus, room_minus = LIMIT_DEG - (j["start"] or 0), (j["start"] or 0) + LIMIT_DEG
        print(f"  J6 started at {fmt(j['start'])} deg  (room: {room_plus:.0f} deg to +90, {room_minus:.0f} deg to -90)")
        print(f"  commanded {fmt(j['cmd'][0])} .. {fmt(j['cmd'][1])} deg, reached {fmt(j['pos'][0])} .. {fmt(j['pos'][1])} deg"
              f"  ({j['source']}, {j['samples']} samples)")
        flag = "  <-- AT THE LIMIT" if j["near_limit_s"] > 0 else ""
        print(f"  within {NEAR_DEG:g} deg of +-90: {j['near_limit_s']:.1f} s{flag}   worst error {fmt(j['max_err'])} deg,"
              f" worst torque {fmt(j['max_torque'], 2)} N.m")
    if pour_n:
        print(f"  pour: max tilt {pour_tilt:.1f} deg ({pour_n} samples)")
    cur.execute(MESSAGES, {"t0": t0, "t1": t1})
    for ts, ev, msg in cur.fetchall():
        if msg:
            print(f"  {ts:%H:%M:%S} {ev}: {msg[:140]}")
    if trace and j and j["source"] == "arm_trace":
        cur.execute(TRACE, {"t0": t0, "t1": t1})
        print("  J6 every 0.5 s (commanded / reached / torque / op):")
        for t, c, p, tau, op in cur.fetchall():
            print(f"    {t:%H:%M:%S.%f}"[:-4] + f"  {fmt(c)}  {fmt(p)}  {fmt(tau, 2)}  {op or ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--run", default=None, help="one run id, with its J6 trace")
    a = ap.parse_args()
    url = dsn()
    if not url:
        print(f"no Tiger URL: put a {tiger.ENV_DSN}=postgres://... line in ~/laminar/.env (or export it), then rerun")
        return 1
    conn = tiger.connect(url)
    with conn.cursor() as cur:
        if a.run:
            cur.execute("SELECT run_id, name, started, ended, status FROM arm_runs WHERE run_id = %s", (a.run,))
        else:
            cur.execute("SELECT run_id, name, started, ended, status FROM arm_runs ORDER BY started DESC LIMIT %s",
                        (a.runs,))
        runs = cur.fetchall()
        if not runs:
            print("no runs in arm_runs yet")
        for r in runs:
            report_run(cur, r, trace=bool(a.run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
