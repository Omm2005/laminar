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

    export TIGER_DATABASE_URL="postgres://tsdbadmin:...@....tsdb.cloud.timescale.com:5432/tsdb?sslmode=require"
    python -m laminar.tiger init                     # create tables, aggregates, policies (idempotent)
    python -m laminar.tiger backfill                 # load logs/telemetry.jsonl
    python -m laminar.tiger arm                      # sample arm_status.json at 10 Hz while arm_server runs
    python -m laminar.tiger report [--minutes 10]    # pours and arm tracking from the aggregates

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
    "ALTER TABLE laminar_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'event')",
    "SELECT add_compression_policy('laminar_events', INTERVAL '1 day', if_not_exists => TRUE)",
    "ALTER TABLE pour_samples SET (timescaledb.compress, timescaledb.compress_segmentby = 'run_id')",
    "SELECT add_compression_policy('pour_samples', INTERVAL '1 day', if_not_exists => TRUE)",
    "ALTER TABLE arm_samples SET (timescaledb.compress, timescaledb.compress_segmentby = 'joint')",
    "SELECT add_compression_policy('arm_samples', INTERVAL '1 day', if_not_exists => TRUE)",
]

INSERT_EVENT = "INSERT INTO laminar_events (ts, run_id, event, data) VALUES (to_timestamp(%s), %s, %s, %s::jsonb)"
INSERT_POUR = ("INSERT INTO pour_samples (ts, run_id, t, tilt, ml, flow, state, action) "
               "VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s)")
INSERT_ARM = ("INSERT INTO arm_samples (ts, joint, cmd_deg, state_deg, err_deg, torque, loop_hz, doing) "
              "VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s)")


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
    ap.add_argument("command", choices=["init", "backfill", "arm", "report"])
    ap.add_argument("path", nargs="?", help="backfill: telemetry JSONL (default logs/telemetry.jsonl)")
    ap.add_argument("--hz", type=float, default=10.0, help="arm: sampling rate")
    ap.add_argument("--minutes", type=float, default=10.0, help="report: look-back window")
    a = ap.parse_args()
    try:
        conn = connect()
    except RuntimeError as e:
        print(e)
        return 1

    if a.command == "init":
        init_schema(conn)
        print("schema ready: laminar_events, pour_samples, arm_samples, pour_1s, arm_tracking_1s, runs")
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
