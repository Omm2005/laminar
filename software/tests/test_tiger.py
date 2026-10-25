import json
import time

from laminar import telemetry, tiger


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.db.executed.append((sql, params))

    def executemany(self, sql, rows):
        if self.db.fail:
            self.db.fail -= 1
            raise ConnectionError("tiger unreachable")
        self.db.rows.setdefault(sql, []).extend(rows)


class FakeConn:
    def __init__(self, fail=0):
        self.rows, self.executed, self.fail, self.closed = {}, [], fail, False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def test_schema_has_hypertables_aggregates_and_compression():
    db = FakeConn()
    tiger.init_schema(db)
    sql = "\n".join(s for s, _ in db.executed)
    for table in ("laminar_events", "pour_samples", "arm_samples"):
        assert f"create_hypertable('{table}'" in sql and f"add_compression_policy('{table}'" in sql
    for view in ("pour_1s", "arm_tracking_1s"):
        assert f"MATERIALIZED VIEW IF NOT EXISTS {view}" in sql and f"add_continuous_aggregate_policy('{view}'" in sql


def test_pour_samples_get_a_typed_row():
    rec = {"ts": 1.0, "event": "pour.sample", "run_id": "r1", "t": 0.5, "tilt": 0.9, "ml": 0.4, "flow": 0.8,
           "state": "pouring", "action": "hold"}
    rows = tiger.event_rows(rec)
    assert [sql for sql, _ in rows] == [tiger.INSERT_EVENT, tiger.INSERT_POUR]
    assert json.loads(rows[0][1][3])["ml"] == 0.4
    assert rows[1][1] == (1.0, "r1", 0.5, 0.9, 0.4, 0.8, "pouring", "hold")


def test_arm_rows_carry_the_tracking_error():
    status = {"command": {"J1": -12.9, "J2": 49.2}, "state": {"J1": -12.5, "J2": 49.0, "J7": 35.0},
              "torque": {"J1": 0.4, "J2": 3.1}, "loop_hz": 98.0, "doing": "path"}
    rows = tiger.arm_rows(status, 5.0)
    assert [r[1] for r in rows] == [(5.0, 1, -12.9, -12.5, -0.4, 0.4, 98.0, "path"),
                                    (5.0, 2, 49.2, 49.0, 0.2, 3.1, 98.0, "path")]


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_forwarder_writes_telemetry_in_the_background(tmp_log):
    db = FakeConn()
    sink = tiger.TigerSink(lambda: db, flush_s=0.05)
    telemetry.add_forwarder(sink.put_event)
    try:
        telemetry.log("executor.report", {"status": "done", "duration_s": 1.5})
        assert _wait(lambda: sink.written == 1)
    finally:
        telemetry._forwarders.remove(sink.put_event)
        sink.close()
    (params,) = db.rows[tiger.INSERT_EVENT]
    assert params[2] == "executor.report" and json.loads(params[3])["status"] == "done"


def test_database_outage_never_blocks_or_raises():
    db = FakeConn(fail=1)
    sink = tiger.TigerSink(lambda: db, flush_s=0.05)
    sink.put_event({"ts": 1.0, "event": "a"})
    assert _wait(lambda: sink.errors == 1)          # first batch lost while the database is down
    sink.put_event({"ts": 2.0, "event": "b"})
    assert _wait(lambda: sink.written == 1)         # reconnects and carries on
    sink.put_event({"bad": "record"})               # malformed: counted, never raised
    sink.close()
    assert sink.stats()["dropped"] == 1 and sink.errors == 2


def test_install_is_a_no_op_without_a_dsn(monkeypatch):
    monkeypatch.delenv(tiger.ENV_DSN, raising=False)
    assert tiger.install() is None


def test_arm_sampler_records_each_new_status(tmp_path):
    f = tmp_path / "arm_status.json"
    f.write_text(json.dumps({"command": {"J1": 1.0}, "state": {"J1": 0.5}, "torque": {}, "loop_hz": 100}))
    db = FakeConn()
    sink = tiger.TigerSink(lambda: db, flush_s=0.05)
    n = tiger.sample_arm(sink, f, hz=50, duration_s=0.2)
    sink.close()
    assert n == 1 and db.rows[tiger.INSERT_ARM][0][4] == 0.5   # unchanged file: sampled once


def _trace_line(t, cmd=1.0, pos=0.5):
    return json.dumps({"t": t, "cmd": [cmd] * 6, "pos": [pos] * 6, "vel": [2.0] * 6, "tau": [0.1] * 6,
                       "hz": 100.0, "op": "path"})


def test_schema_has_run_trace_tables_and_functions():
    db = FakeConn()
    tiger.init_schema(db)
    sql = "\n".join(s for s, _ in db.executed)
    assert "create_hypertable('arm_trace'" in sql and "add_compression_policy('arm_trace'" in sql
    assert "CREATE TABLE IF NOT EXISTS arm_runs" in sql
    assert "FUNCTION run_smoothness" in sql and "FUNCTION run_trace" in sql


def test_trace_rows_one_per_joint_inside_the_window():
    rec = json.loads(_trace_line(10.0, cmd=3.0, pos=2.5))
    rows = tiger.trace_rows(rec, t0=9.0, t1=11.0)
    assert len(rows) == 6 and rows[0] == (tiger.INSERT_TRACE, (10.0, 1, 3.0, 2.5, 0.5, 2.0, 0.1, 100.0, "path"))
    assert tiger.trace_rows(rec, t0=10.5) == [] and tiger.trace_rows(rec, t1=9.5) == []


def test_trace_tail_skips_fragments_and_follows_rollover(tmp_path):
    f = tmp_path / "arm_trace.jsonl"
    f.write_text(_trace_line(1.0) + "\n" + _trace_line(2.0)[:20])   # second line still being written
    db = FakeConn()
    sink = tiger.TigerSink(lambda: db, flush_s=0.05)
    tail = tiger.TraceTail(sink, f, t0=0.0)
    assert tail.poll() == 1
    with f.open("a") as fh:
        fh.write(_trace_line(2.0)[20:] + "\n")
    assert tail.poll() == 1                                          # the completed line, once
    f.write_text(_trace_line(3.0) + "\n")                            # rolled over: smaller file
    assert tail.poll() == 1
    sink.close()
    assert [r[0] for r in db.rows[tiger.INSERT_TRACE][::6]] == [1.0, 2.0, 3.0]


def test_run_logger_records_the_run_and_only_its_trace(tmp_path):
    f = tmp_path / "arm_trace.jsonl"
    now = time.time()
    f.write_text(_trace_line(now - 30) + "\n")                       # long before the run: not uploaded
    db = FakeConn()
    run = tiger.RunLogger(tiger.TigerSink(lambda: db, flush_s=0.05), "job1", "vial_A -> well_3",
                          {"vial": 1}, trace_path=f, poll_s=0.02, settle_s=0.05)
    with f.open("a") as fh:
        fh.write(_trace_line(time.time()) + "\n")
    run.event("step.start", {"name": "lift"})
    stats = run.finish("done")
    assert stats["trace_lines"] == 1 and len(db.rows[tiger.INSERT_TRACE]) == 6
    start, end = db.rows[tiger.UPSERT_RUN]
    assert start[0] == "job1" and start[3] is None and start[4] == "running"
    assert end[3] is not None and end[4] == "done"
    events = [p[2] for p in db.rows[tiger.INSERT_EVENT]]
    assert events == ["step.start", "run.end"] and all(p[1] == "job1" for p in db.rows[tiger.INSERT_EVENT])


def test_run_logger_is_off_without_a_dsn(monkeypatch):
    monkeypatch.delenv(tiger.ENV_DSN, raising=False)
    assert tiger.RunLogger.start("job1") is None


def test_recording_import_differentiates_velocity(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"fps": 20.0, "started": "2026-10-23 21:27:20"}))
    head = "frame,t," + ",".join(f"{k}.J{j}" for k in ("action", "command", "state") for j in range(1, 8))
    line = lambda i, t, x: f"{i},{t}," + ",".join([str(x)] * 21)
    (tmp_path / "data.csv").write_text("\n".join([head, line(0, 0.0, 0.0), line(1, 0.05, 1.0)]) + "\n")
    run_id, t0, t1, rows = tiger.recording_rows(tmp_path)
    assert run_id == f"rec-{tmp_path.name}" and abs(t1 - t0 - 0.05) < 1e-6 and len(rows) == 12
    assert abs(rows[6][1][5] - 20.0) < 1e-3 and rows[6][1][8] == "teleop"   # 1 deg in 50 ms
