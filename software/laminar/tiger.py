"""Tiger Data (TimescaleDB on Postgres): every Laminar event, pour sample and arm tracking sample, as time series.

Three hypertables, written in the background so nothing on the bench ever waits for the network:
    laminar_events  every telemetry.log() event (executor, skills, safety, pour), JSONB payload
    pour_samples    typed copy of each pour.sample (tilt, mL, flow) for fast pour analytics
    arm_samples     per-joint commanded vs measured angle, tracking error, torque and loop rate
                    from robotics/arm_status.json (how we found the base's stick-slip)
Continuous aggregates (real-time, so dashboards read pre-computed buckets plus the newest rows):
    pour_1s           per run, per second: mL, flow, max tilt
    arm_tracking_1s   per joint, per second: mean / max |error|, error stddev (stick-slip), loop rate
    runs              plain view: one row per finished executor run (status, duration)
Compression after 1 day (segmented by event / run / joint) keeps a free-tier service small.

Runs (for trajectory smoothing): every run is a row in arm_runs (start, end, status), and while it is
active the 100 Hz trace from robotics/arm_trace.py goes to the arm_trace hypertable. Trace rows are
matched to runs by time, so nothing has to be tagged. run_smoothness(t0, t1) and run_trace(t0, t1, bucket)
are SQL functions shared by the report here and the console's Runs page.

    export TIGER_DATABASE_URL="postgres://tsdbadmin:...@....tsdb.cloud.timescale.com:5432/tsdb?sslmode=require"
    python -m laminar.tiger init                     # create tables, aggregates, policies (idempotent)
    python -m laminar.tiger backfill                 # load logs/telemetry.jsonl
    python -m laminar.tiger arm                      # sample arm_status.json at 10 Hz while arm_server runs
    python -m laminar.tiger report [--minutes 10]    # pours and arm tracking from the aggregates
    python -m laminar.tiger run [--name N] -- python transfer.py vial_A well_3   # log any command as a run
    python -m laminar.tiger smooth [--run ID]        # per-joint tracking error, jerk, stalls (default: last run)
    python -m laminar.tiger import recordings/2026...  # a teleop recording (recorder.py) as a run

In code (forwards every telemetry.log() event from then on; the local JSONL stays the source of truth):
    from laminar import tiger; tiger.install()
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from . import telemetry

ENV_DSN = "TIGER_DATABASE_URL"
ARM_STATUS = telemetry.REPO_ROOT / "robotics" / "arm_status.json"
ARM_TRACE = telemetry.REPO_ROOT / "robotics" / "logs" / "arm_trace.jsonl"
TRACE_PAD_S = 1.0  # trace kept before/after a run so the start and the settle are in the data

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS laminar_events (
        ts TIMESTAMPTZ NOT NULL, run_id TEXT, event TEXT NOT NULL, data JSONB NOT NULL)""",
    "SELECT create_hypertable('laminar_events', by_range('ts'), if_not_exists => TRUE)",
    "CREATE INDEX IF NOT EXISTS laminar_events_run_ts ON laminar_events (run_id, ts DESC)",
    """CREATE TABLE IF NOT EXISTS pour_samples (
        ts TIMESTAMPTZ NOT NULL, run_id TEXT, t REAL, tilt REAL, ml REAL, flow REAL, state TEXT, action TEXT)""",
    "SELECT create_hypertable('pour_samples', by_range('ts'), if_not_exists => TRUE)",
    """CREATE TABLE IF NOT EXISTS arm_samples (
        ts TIMESTAMPTZ NOT NULL, joint SMALLINT NOT NULL, cmd_deg REAL, state_deg REAL, err_deg REAL,
        torque REAL, loop_hz REAL, doing TEXT)""",
    "SELECT create_hypertable('arm_samples', by_range('ts'), if_not_exists => TRUE)",
    """CREATE MATERIALIZED VIEW IF NOT EXISTS pour_1s
        WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
        SELECT time_bucket('1 second', ts) AS bucket, run_id,
               max(ml) AS ml, avg(flow) AS flow, max(tilt) AS max_tilt, count(*) AS n
        FROM pour_samples GROUP BY bucket, run_id WITH NO DATA""",
    """CREATE MATERIALIZED VIEW IF NOT EXISTS arm_tracking_1s
        WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
        SELECT time_bucket('1 second', ts) AS bucket, joint,
               avg(abs(err_deg)) AS mean_abs_err, max(abs(err_deg)) AS max_abs_err,
               stddev(err_deg) AS err_std, avg(loop_hz) AS loop_hz, count(*) AS n
        FROM arm_samples GROUP BY bucket, joint WITH NO DATA""",
    """SELECT add_continuous_aggregate_policy('pour_1s', start_offset => INTERVAL '1 day',
        end_offset => INTERVAL '1 second', schedule_interval => INTERVAL '30 seconds', if_not_exists => TRUE)""",
    """SELECT add_continuous_aggregate_policy('arm_tracking_1s', start_offset => INTERVAL '1 day',
        end_offset => INTERVAL '1 second', schedule_interval => INTERVAL '30 seconds', if_not_exists => TRUE)""",
    """CREATE OR REPLACE VIEW runs AS
        SELECT ts, run_id, data->>'status' AS status, (data->>'duration_s')::real AS duration_s
        FROM laminar_events WHERE event = 'executor.report'""",
    """CREATE TABLE IF NOT EXISTS arm_trace (
        ts TIMESTAMPTZ NOT NULL, joint SMALLINT NOT NULL, cmd_deg REAL, pos_deg REAL, err_deg REAL,
        vel_dps REAL, torque REAL, loop_hz REAL, op TEXT)""",
    "SELECT create_hypertable('arm_trace', by_range('ts', INTERVAL '1 hour'), if_not_exists => TRUE)",
    "CREATE INDEX IF NOT EXISTS arm_trace_joint_ts ON arm_trace (joint, ts DESC)",
    """CREATE TABLE IF NOT EXISTS arm_runs (
        run_id TEXT PRIMARY KEY, name TEXT, started TIMESTAMPTZ NOT NULL, ended TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'running', meta JSONB NOT NULL DEFAULT '{}'::jsonb)""",
    "CREATE INDEX IF NOT EXISTS arm_runs_started ON arm_runs (started DESC)",
    # 50 ms buckets: the motor velocity is noisy at 100 Hz and jerk from raw samples is all noise.
    # stall_frac: share of the time the command moves (> 2 deg/s) but the joint does not (< 0.5 deg/s),
    # i.e. stick-slip.
    """CREATE OR REPLACE FUNCTION run_smoothness(t0 TIMESTAMPTZ, t1 TIMESTAMPTZ)
    RETURNS TABLE (joint SMALLINT, samples BIGINT, rms_err REAL, max_err REAL, peak_vel REAL,
                   peak_acc REAL, rms_jerk REAL, stall_frac REAL, peak_torque REAL)
    LANGUAGE sql STABLE AS $fn$
    WITH b AS (
        SELECT time_bucket('50 milliseconds', ts) AS t, a.joint, avg(cmd_deg) AS cmd, avg(err_deg) AS err,
               avg(vel_dps) AS vel, max(abs(torque)) AS tau, count(*) AS n
        FROM arm_trace a WHERE ts >= t0 AND ts <= t1 GROUP BY 1, 2),
    d AS (
        SELECT *, extract(epoch FROM t - lag(t) OVER w) AS dt, vel - lag(vel) OVER w AS dv,
               cmd - lag(cmd) OVER w AS dcmd
        FROM b WINDOW w AS (PARTITION BY joint ORDER BY t)),
    a AS (SELECT *, dv / dt AS acc, dcmd / dt AS cmd_vel FROM d WHERE dt > 0 AND dt < 0.2),
    j AS (SELECT *, (acc - lag(acc) OVER w) / dt AS jerk FROM a WINDOW w AS (PARTITION BY joint ORDER BY t))
    SELECT joint, sum(n)::bigint, sqrt(avg(err * err))::real, max(abs(err))::real, max(abs(vel))::real,
           max(abs(acc))::real, sqrt(avg(jerk * jerk))::real,
           (avg((abs(vel) < 0.5)::int) FILTER (WHERE abs(cmd_vel) > 2))::real, max(tau)::real
    FROM j GROUP BY joint ORDER BY joint
    $fn$""",
    """CREATE OR REPLACE FUNCTION run_trace(t0 TIMESTAMPTZ, t1 TIMESTAMPTZ, bucket INTERVAL)
    RETURNS TABLE (t DOUBLE PRECISION, joint SMALLINT, cmd REAL, pos REAL, err REAL, vel REAL, torque REAL)
    LANGUAGE sql STABLE AS $fn$
    SELECT extract(epoch FROM time_bucket(bucket, ts)) * 1000, joint, avg(cmd_deg)::real, avg(pos_deg)::real,
           avg(err_deg)::real, avg(vel_dps)::real, avg(torque)::real
    FROM arm_trace WHERE ts >= t0 AND ts <= t1 GROUP BY 1, 2 ORDER BY 1, 2
    $fn$""",
    "ALTER TABLE laminar_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'event')",
    "SELECT add_compression_policy('laminar_events', INTERVAL '1 day', if_not_exists => TRUE)",
    "ALTER TABLE pour_samples SET (timescaledb.compress, timescaledb.compress_segmentby = 'run_id')",
    "SELECT add_compression_policy('pour_samples', INTERVAL '1 day', if_not_exists => TRUE)",
    "ALTER TABLE arm_samples SET (timescaledb.compress, timescaledb.compress_segmentby = 'joint')",
    "SELECT add_compression_policy('arm_samples', INTERVAL '1 day', if_not_exists => TRUE)",
    "ALTER TABLE arm_trace SET (timescaledb.compress, timescaledb.compress_segmentby = 'joint')",
    "SELECT add_compression_policy('arm_trace', INTERVAL '1 day', if_not_exists => TRUE)",
]

INSERT_EVENT = "INSERT INTO laminar_events (ts, run_id, event, data) VALUES (to_timestamp(%s), %s, %s, %s::jsonb)"
INSERT_POUR = ("INSERT INTO pour_samples (ts, run_id, t, tilt, ml, flow, state, action) "
               "VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s)")
INSERT_ARM = ("INSERT INTO arm_samples (ts, joint, cmd_deg, state_deg, err_deg, torque, loop_hz, doing) "
              "VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s)")

INSERT_TRACE = ("INSERT INTO arm_trace (ts, joint, cmd_deg, pos_deg, err_deg, vel_dps, torque, loop_hz, op) "
                "VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s)")
UPSERT_RUN = ("INSERT INTO arm_runs (run_id, name, started, ended, status, meta) "
              "VALUES (%s, %s, to_timestamp(%s), to_timestamp(%s), %s, %s::jsonb) "
              "ON CONFLICT (run_id) DO UPDATE SET ended = EXCLUDED.ended, status = EXCLUDED.status, "
              "meta = arm_runs.meta || EXCLUDED.meta")


def connect(dsn: str | None = None):
    """psycopg connection to the Tiger service (autocommit: SCHEMA has statements that can't run in a transaction)."""
    dsn = dsn or os.environ.get(ENV_DSN)
    if not dsn:
        raise RuntimeError(f"set {ENV_DSN} to the Tiger service URL (Tiger console -> service -> connect)")
    import psycopg
    return psycopg.connect(dsn, autocommit=True)


def init_schema(conn) -> None:
    with conn.cursor() as cur:
        for sql in SCHEMA:
            cur.execute(sql)


# ---------------------------------------------------------------- rows

def event_rows(rec: dict) -> list[tuple[str, tuple]]:
    """A telemetry record -> (insert sql, params) rows: always one event row, plus a typed pour row."""
    data = {k: v for k, v in rec.items() if k not in ("ts", "run_id", "event")}
    rows = [(INSERT_EVENT, (rec["ts"], rec.get("run_id"), rec["event"],
                            json.dumps(data, default=telemetry._jsonable)))]
    if rec["event"] == "pour.sample":
        rows.append((INSERT_POUR, (rec["ts"], rec.get("run_id"), data.get("t"), data.get("tilt"), data.get("ml"),
                                   data.get("flow"), data.get("state"), data.get("action"))))
    return rows


def arm_rows(status: dict, ts: float) -> list[tuple[str, tuple]]:
    """One arm_status.json snapshot -> a row per arm joint (J1-J6) with its tracking error."""
    cmd, state, torque = status.get("command") or {}, status.get("state") or {}, status.get("torque") or {}
    doing = status.get("doing") or status.get("op")
    rows = []
    for name, c in cmd.items():
        s = state.get(name)
        if s is None:
            continue
        rows.append((INSERT_ARM, (ts, int(name[1:]), c, s, round(c - s, 3), torque.get(name),
                                  status.get("loop_hz"), doing)))
    return rows


def trace_rows(rec: dict, t0: float | None = None, t1: float | None = None) -> list[tuple[str, tuple]]:
    """One arm_trace.jsonl line -> a row per joint J1-J6; [] if outside [t0, t1]."""
    ts = rec["t"]
    if (t0 is not None and ts < t0) or (t1 is not None and ts > t1):
        return []
    return [(INSERT_TRACE, (ts, j + 1, c, p, round(c - p, 3), v, tau, rec.get("hz"), rec.get("op")))
            for j, (c, p, v, tau) in enumerate(zip(rec["cmd"], rec["pos"], rec["vel"], rec["tau"]))]


# ---------------------------------------------------------------- background writer

class TigerSink:
    """Queues rows and writes them in batches on its own thread. put() never blocks and never raises:
    if the queue is full (database down) rows are dropped and counted, and the bench keeps running."""

    def __init__(self, conn_factory: Callable[[], object], batch: int = 500, flush_s: float = 0.5,
                 max_queue: int = 50_000):
        self.conn_factory, self.batch, self.flush_s = conn_factory, batch, flush_s
        self.q: queue.Queue = queue.Queue(maxsize=max_queue)
        self.dropped = self.written = self.errors = 0
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="tiger-sink", daemon=True)
        self._thread.start()

    def put_rows(self, rows: Iterable[tuple[str, tuple]]) -> None:
        for row in rows:
            try:
                self.q.put_nowait(row)
            except queue.Full:
                self.dropped += 1

    def put_event(self, rec: dict) -> None:
        """telemetry forwarder."""
        try:
            self.put_rows(event_rows(rec))
        except Exception as e:  # a malformed record must never break telemetry.log()
            self.errors += 1
            self.last_error = repr(e)

    def _drain(self) -> list[tuple[str, tuple]]:
        rows = []
        try:
            rows.append(self.q.get(timeout=self.flush_s))
            while len(rows) < self.batch:
                rows.append(self.q.get_nowait())
        except queue.Empty:
            pass
        return rows

    def _loop(self) -> None:
        conn = None
        while not (self._stop.is_set() and self.q.empty()):
            rows = self._drain()
            if not rows:
                continue
            by_sql: dict[str, list[tuple]] = {}
            for sql, params in rows:
                by_sql.setdefault(sql, []).append(params)
            try:
                if conn is None:
                    conn = self.conn_factory()
                with conn.cursor() as cur:
                    for sql, params in by_sql.items():
                        cur.executemany(sql, params)
                self.written += len(rows)
            except Exception as e:  # network blip, service paused: drop this batch, reconnect next time
                self.errors += 1
                self.dropped += len(rows)
                self.last_error = repr(e)
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
                time.sleep(1.0)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close(self, timeout: float = 5.0) -> None:
        """Flush what is queued (up to timeout), then stop."""
        self._stop.set()
        self._thread.join(timeout)

    def stats(self) -> dict:
        return {"written": self.written, "queued": self.q.qsize(), "dropped": self.dropped,
                "errors": self.errors, "last_error": self.last_error}


def install(dsn: str | None = None) -> TigerSink | None:
    """Forward every telemetry.log() event to Tiger from now on. None (and nothing changes) without a DSN."""
    dsn = dsn or os.environ.get(ENV_DSN)
    if not dsn:
        return None
    sink = TigerSink(lambda: connect(dsn))
    telemetry.add_forwarder(sink.put_event)
    return sink


# ---------------------------------------------------------------- sources

def backfill(sink: TigerSink, path: str | Path | None = None) -> int:
    recs = telemetry.read(path)
    for rec in recs:
        sink.put_event(rec)
    return len(recs)


def sample_arm(sink: TigerSink, status_file: Path = ARM_STATUS, hz: float = 10.0,
               duration_s: float | None = None, clock=time) -> int:
    """Record arm_server's status (written every 0.2 s while moving, 0.5 s idle) whenever it changes."""
    last_mtime, n = None, 0
    end = None if duration_s is None else clock.time() + duration_s
    while end is None or clock.time() < end:
        try:
            mtime = status_file.stat().st_mtime
            if mtime != last_mtime:
                status = json.loads(status_file.read_text())
                last_mtime = mtime
                rows = arm_rows(status, mtime)
                sink.put_rows(rows)
                n += len(rows)
        except (OSError, ValueError):
            pass  # not running yet, or caught mid-write: try again next tick
        clock.sleep(1.0 / hz)
    return n


class TraceTail:
    """Follows arm_trace.jsonl from just before its current end and queues the lines inside [t0, t1].
    Handles the file rolling over and a line caught mid-write."""

    BACKLOG_BYTES = 200_000  # ~2 s at 100 Hz, so the TRACE_PAD_S before the run is still there

    def __init__(self, sink: TigerSink, path: Path = ARM_TRACE, t0: float | None = None):
        self.sink, self.path, self.t0, self.t1 = sink, path, t0, None
        self.pos, self.buf, self.lines = 0, "", 0
        try:
            self.pos = max(0, path.stat().st_size - self.BACKLOG_BYTES)
        except OSError:
            pass
        self.skip_partial = self.pos > 0  # started mid-file: the first line is a fragment

    def poll(self) -> int:
        try:
            if self.path.stat().st_size < self.pos:  # rolled over: start the new file
                self.pos, self.buf, self.skip_partial = 0, "", False
            with self.path.open("rb") as f:
                f.seek(self.pos)
                chunk = f.read().decode("utf-8", "replace")
                self.pos = f.tell()
        except OSError:
            return 0
        *lines, self.buf = (self.buf + chunk).split("\n")
        if self.skip_partial and lines:
            lines, self.skip_partial = lines[1:], False
        n = 0
        for line in lines:
            try:
                rows = trace_rows(json.loads(line), self.t0, self.t1)
            except (ValueError, KeyError, TypeError):
                continue
            if rows:
                self.sink.put_rows(rows)
                n += 1
        self.lines += n
        return n


class RunLogger:
    """One run in Tiger: an arm_runs row, events tagged with its run_id, and the arm trace while it lasts.

        run = RunLogger.start(job_id, name="vial_A -> well_3", meta={...})   # None without a DSN
        run.event("step.start", {"name": "lift"})
        run.finish("done")

    Never blocks or raises into the caller: everything goes through a TigerSink."""

    def __init__(self, sink: TigerSink, run_id: str, name: str | None = None, meta: dict | None = None,
                 trace_path: Path = ARM_TRACE, poll_s: float = 0.25, settle_s: float = 0.8, clock=time):
        self.sink, self.run_id, self.name, self.settle_s, self.clock = sink, run_id, name, settle_s, clock
        self.t0 = clock.time()
        self.sink.put_rows([(UPSERT_RUN, (run_id, name, self.t0, None, "running", json.dumps(meta or {})))])
        self.tail = TraceTail(sink, trace_path, self.t0 - TRACE_PAD_S)
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="tiger-trace", daemon=True)
        self._thread.start()

    @classmethod
    def start(cls, run_id: str, name: str | None = None, meta: dict | None = None, dsn: str | None = None,
              **kw) -> "RunLogger | None":
        dsn = dsn or os.environ.get(ENV_DSN)
        if not dsn:
            return None
        return cls(TigerSink(lambda: connect(dsn)), run_id, name, meta, **kw)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tail.poll()
            self._stop.wait(self.poll_s)

    def event(self, event: str, data: dict | None = None) -> None:
        self.sink.put_event({"ts": self.clock.time(), "run_id": self.run_id, "event": event, **(data or {})})

    def finish(self, status: str, meta: dict | None = None, timeout: float = 15.0) -> dict:
        t1 = self.clock.time()
        self.clock.sleep(self.settle_s)  # arm_trace flushes every 0.2 s: wait for the settle after the last move
        self._stop.set()
        self._thread.join()
        self.tail.t1 = t1 + TRACE_PAD_S
        self.tail.poll()
        self.event("run.end", {"status": status, "duration_s": round(t1 - self.t0, 2)})
        self.sink.put_rows([(UPSERT_RUN, (self.run_id, self.name, self.t0, t1, status, json.dumps(meta or {})))])
        self.sink.close(timeout)
        return {**self.sink.stats(), "trace_lines": self.tail.lines}


def recording_rows(folder: str | Path) -> tuple[str, float, float, list[tuple[str, tuple]]]:
    """A robotics/recorder.py episode (data.csv + meta.json) -> (run_id, t0, t1, trace rows). The follower
    command is "cmd", the measured follower position "pos"; velocity is differentiated from pos (the
    recorder has no motor velocity or torque)."""
    import csv
    from datetime import datetime
    folder = Path(folder)
    meta = json.loads((folder / "meta.json").read_text())
    start = datetime.strptime(meta["started"], "%Y-%m-%d %H:%M:%S").timestamp()
    rows, prev = [], None
    with (folder / "data.csv").open() as f:
        for r in csv.DictReader(f):
            t = start + float(r["t"])
            cmd = [float(r[f"command.J{j}"]) for j in range(1, 7)]
            pos = [float(r[f"state.J{j}"]) for j in range(1, 7)]
            vel = [0.0] * 6 if prev is None or t <= prev[0] else [(p - q) / (t - prev[0]) for p, q in zip(pos, prev[1])]
            prev = (t, pos)
            rows += trace_rows({"t": t, "cmd": cmd, "pos": pos, "vel": vel, "tau": [None] * 6,
                                "hz": meta.get("fps"), "op": "teleop"})
    t0, t1 = rows[0][1][0], rows[-1][1][0]
    return f"rec-{folder.name}", t0, t1, rows


SMOOTH_SQL = ("SELECT r.run_id, r.name, r.status, r.started, r.ended, s.* FROM arm_runs r, "
              "LATERAL run_smoothness(r.started - interval '1 second', "
              "coalesce(r.ended, now()) + interval '1 second') s "
              "WHERE r.run_id = coalesce(%s, (SELECT run_id FROM arm_runs ORDER BY started DESC LIMIT 1)) "
              "ORDER BY s.joint")


def smoothness(conn, run_id: str | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(SMOOTH_SQL, (run_id,))
        return [tuple(r) for r in cur.fetchall()]


REPORT_SQL = {
    "runs": "SELECT ts, run_id, status, duration_s FROM runs WHERE ts > now() - %s * INTERVAL '1 minute' "
            "ORDER BY ts DESC LIMIT 20",
    "pours": "SELECT run_id, max(ml) AS ml, max(max_tilt) AS max_tilt, count(*) AS seconds FROM pour_1s "
             "WHERE bucket > now() - %s * INTERVAL '1 minute' GROUP BY run_id ORDER BY max(bucket) DESC LIMIT 20",
    "arm": "SELECT joint, round(avg(mean_abs_err)::numeric, 2) AS mean_abs_err, "
           "round(max(max_abs_err)::numeric, 2) AS max_abs_err, round(avg(err_std)::numeric, 2) AS err_std, "
           "round(avg(loop_hz)::numeric, 1) AS loop_hz FROM arm_tracking_1s "
           "WHERE bucket > now() - %s * INTERVAL '1 minute' GROUP BY joint ORDER BY joint",
}


def report(conn, minutes: float = 10.0) -> dict[str, list[tuple]]:
    out = {}
    with conn.cursor() as cur:
        for name, sql in REPORT_SQL.items():
            cur.execute(sql, (minutes,))
            out[name] = [tuple(r) for r in cur.fetchall()]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["init", "backfill", "arm", "report", "run", "smooth", "import"])
    ap.add_argument("path", nargs="?", help="backfill: telemetry JSONL (default logs/telemetry.jsonl); "
                                            "import: a recorder.py episode folder")
    ap.add_argument("--name", help="run: label shown on the console's Runs page")
    ap.add_argument("--run", help="smooth: run id (default: the latest run)")
    ap.add_argument("--hz", type=float, default=10.0, help="arm: sampling rate")
    ap.add_argument("--minutes", type=float, default=10.0, help="report: look-back window")
    argv = sys.argv[1:]
    cut = argv.index("--") if "--" in argv else len(argv)  # run: everything after -- is the command
    a = ap.parse_args(argv[:cut])
    a.cmd = argv[cut + 1:]
    try:
        conn = connect()
    except RuntimeError as e:
        print(e)
        return 1

    if a.command == "init":
        init_schema(conn)
        print("schema ready: laminar_events, pour_samples, arm_samples, pour_1s, arm_tracking_1s, runs")
    elif a.command == "run":
        cmd = a.cmd
        if not cmd:
            print("usage: python -m laminar.tiger run [--name NAME] -- <command ...>")
            return 2
        import subprocess
        import uuid
        run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        run = RunLogger(TigerSink(connect), run_id, a.name or " ".join(cmd)[:120],
                        {"cmd": cmd, "cwd": os.getcwd()})
        print(f"tiger run {run_id}: {' '.join(cmd)}", flush=True)
        os.environ["LAMINAR_RUN_ID"] = run_id  # the arm scripts inside log under this run, not their own
        status, code = "failed", None
        try:
            code = subprocess.call(cmd)
            status = "done" if code == 0 else "failed"
        except KeyboardInterrupt:
            status = "aborted"
        finally:
            print("tiger:", run.finish(status, {"exit_code": code}), flush=True)
        conn.close()
        return code or 0
    elif a.command == "import":
        if not a.path:
            print("usage: python -m laminar.tiger import <recording folder>")
            return 2
        run_id, t0, t1, rows = recording_rows(a.path)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM arm_trace WHERE ts BETWEEN to_timestamp(%s) AND to_timestamp(%s) AND op = 'teleop'",
                        (t0, t1))
            cur.executemany(INSERT_TRACE, [p for _, p in rows])
            cur.execute(UPSERT_RUN, (run_id, a.name or f"teleop recording {Path(a.path).name}", t0, t1, "done",
                                     json.dumps({"source": "recorder.py", "folder": str(a.path)})))
        print(f"{run_id}: {len(rows) // 6} samples, {t1 - t0:.1f} s")
    elif a.command == "smooth":
        rows = smoothness(conn, a.run)
        if not rows:
            print("no runs with an arm trace yet")
        for i, r in enumerate(rows):
            if i == 0:
                end = f"{r[4].astimezone():%H:%M:%S}" if r[4] else "running"
                print(f"run {r[0]} ({r[1]}) {r[2]}  {r[3].astimezone():%H:%M:%S} -> {end}")
                print("joint  samples  rms_err  max_err  peak_vel  peak_acc  rms_jerk  stall%  peak_tau")
            j, n, rms, mx, pv, pa, jk, st, tau = r[5:]
            f = lambda x, spec, w, k=1: f"{x * k:{w}{spec}}" if x is not None else "-".rjust(w)
            print(f"  J{j}  {n:7d}  {f(rms, '.2f', 7)}  {f(mx, '.2f', 7)}  {f(pv, '.1f', 8)}  {f(pa, '.0f', 8)}  "
                  f"{f(jk, '.0f', 8)}  {f(st, '.1f', 5, 100)}  {f(tau, '.2f', 8)}")
    elif a.command == "report":
        for name, rows in report(conn, a.minutes).items():
            print(f"{name}:")
            for r in rows:
                print("  " + "  ".join(str(x) for x in r))
    else:
        sink = TigerSink(connect)
        try:
            if a.command == "backfill":
                print(f"queued {backfill(sink, a.path)} events")
            else:
                print(f"sampling {ARM_STATUS} at {a.hz:g} Hz, Ctrl+C to stop")
                try:
                    sample_arm(sink, hz=a.hz)
                except KeyboardInterrupt:
                    pass
        finally:
            sink.close(timeout=30.0)
            print("tiger:", sink.stats())
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
