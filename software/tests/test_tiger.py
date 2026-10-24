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
