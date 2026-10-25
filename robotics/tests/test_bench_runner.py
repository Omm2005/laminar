"""bench_runner.py without the arm: a fake website and a fake pour_episode.py."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bench_runner as B  # noqa: E402

RUN = {"id": "run_abc123", "tasks": [{"tube": "B", "pours": [{"well": 2, "ml": 1.0}, {"well": 3, "ml": 0.5}]}],
       "moves": ["grab_B", "lift(10)", "travel_to(well2)", "lower(6)", "pour(well2, 1.0 mL)", "lift(6)",
                 "travel_to(well3)", "lower(6)", "pour(well3, 0.5 mL)", "lift(6)", "travel_to(slot_B)", "lower(10)",
                 "release_B"]}


class FakeSite:
    def __init__(self):
        self.updates = []

    def update(self, run_id, **fields):
        self.updates.append(fields)


def test_jobs_map_wells_to_containers_and_ml_to_percent():
    jobs = B.jobs_for(RUN)
    assert [j["job"] for j in jobs] == ["B2b", "B2c"]
    assert jobs[0]["spec"] == f"B2b:+{100 / B.FULL_ML:.1f}"
    assert [j["column"] for j in jobs] == ["B", "C"]


def test_too_much_or_unknown_well_is_refused():
    with pytest.raises(ValueError):
        B.jobs_for({"tasks": [{"tube": "A", "pours": [{"well": 5, "ml": 1}]}]})
    with pytest.raises(ValueError):
        B.jobs_for({"tasks": [{"tube": "A", "pours": [{"well": 1, "ml": 9}]}]})


def test_pour_steps_point_at_the_pour_moves():
    assert B.pour_steps(RUN["moves"]) == [4, 8]


def fake_episode(tmp_path, monkeypatch, summary, rc=0):
    out = tmp_path / "logs" / "episode_x"
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps(summary))
    monkeypatch.setattr(B, "HERE", tmp_path)

    class Proc:
        stdout = io.StringIO("logging to logs/episode_x\n=== B2b: add 23% of column B\n=== B2c: add 12%\n")

        def wait(self):
            return rc
    monkeypatch.setattr(B.subprocess, "Popen", lambda *a, **k: Proc())


ARGS = SimpleNamespace(yes=True, side_cam=2, wrist_cam=1, episode_args=[])


def test_a_good_run_reports_running_steps_pours_and_done(tmp_path, monkeypatch):
    fake_episode(tmp_path, monkeypatch, {"jobs": [
        {"start": {"B": {"ml": 0.2}}, "end": {"B": {"ml": 1.18}}},
        {"start": {"C": {"ml": 0.0}}, "end": {"C": {"ml": 0.52}}}], "error": None})
    site = FakeSite()
    B.run_one(site, RUN, ARGS)
    assert site.updates[0] == {"status": "running", "step": 0}
    assert {"step": 4} in site.updates and {"step": 8} in site.updates
    pours = [u["pour"] for u in site.updates if "pour" in u]
    assert pours == [{"tube": "B", "well": 2, "targetMl": 1.0, "pouredMl": 0.98},
                     {"tube": "B", "well": 3, "targetMl": 0.5, "pouredMl": 0.52}]
    assert site.updates[-1] == {"status": "done"}


def test_a_stopped_episode_reports_aborted_with_the_reason(tmp_path, monkeypatch):
    fake_episode(tmp_path, monkeypatch, {"jobs": [{"start": {"B": {"ml": 0.2}}}],
                                         "error": "B2b: no flow at max tilt"}, rc=0)
    site = FakeSite()
    B.run_one(site, RUN, ARGS)
    assert not [u for u in site.updates if "pour" in u]          # no end level: no measurement reported
    assert site.updates[-1] == {"status": "aborted", "reason": "B2b: no flow at max tilt"}


def test_skipped_at_the_bench(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "s")
    site = FakeSite()
    B.run_one(site, RUN, SimpleNamespace(yes=False, side_cam=2, wrist_cam=1, episode_args=[]))
    assert site.updates == [{"status": "aborted", "reason": "skipped at the bench"}]
