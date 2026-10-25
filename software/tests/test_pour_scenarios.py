"""The camera-steered pour, every situation we can think of, simulated.

Runs the exact loop robotics/pour_episode.py uses: pour_motor6._pour(joint, tracker, column, direction, empty=[]),
with the side camera's tracker replaced by a simulated bench:
    liquid   flows into the target once the wrist passes the onset angle, faster the further it tilts, and keeps
             arriving for a moment after tilt-back (stream lag)
    camera   a reading every 50 ms (20 Hz), the tracker's median of the last 3, optional noise / spikes /
             dropouts / a slow frame rate, like the level model on the drawn boxes
    bench    target A between watched columns x1 (left) and x2 (right) and another container B; any of them can
             already hold spill from earlier pours
Every scenario also checks the safety rules that must hold whatever happens: the wrist never passes max tilt,
it ends upright, and a pour reported done is not overfilled.
"""
from __future__ import annotations

import math
import random
from collections import deque

import pytest

from laminar.pour_motor6 import PourConfig, SimClock, SimJoint, _pour   # (puts sense/ on the path)
import pour_guide  # noqa: E402

BOX_PX, ML_PER_PX = 200.0, 0.02          # a 200 px box holds 4 mL
FULL_ML = BOX_PX * ML_PER_PX


class SimBench:
    """The side camera + level model on the drawn boxes, over simulated liquid. Same interface as the tracker
    pour_episode hands to _pour: .columns (with roi + px_to_ml) and .read(name)."""

    X = {"x1": 60, "A": 100, "x2": 140, "B": 180}             # left to right, px

    def __init__(self, clock: SimClock, joint: SimJoint, direction: int, *, onset_deg=40.0, k=0.05, tau=0.2,
                 start_ml=None, into=None, flow_mult=None, extra=None, noise_px=0.0, spike_p=0.0,
                 conf=None, cam_hz=20.0, seed=0, dry=False):
        from level import Column
        self.clock, self.joint, self.direction = clock, joint, direction
        self.base = joint.position()
        self.onset, self.k, self.tau, self.dry = onset_deg, k, tau, dry
        curve = [(0.0, 0.0), (BOX_PX, FULL_ML)]
        self.columns = {n: Column(n, (x, 0, 30, int(BOX_PX)), curve) for n, x in self.X.items()}
        self.ml = {n: 0.0 for n in self.columns} | (start_ml or {})
        self.into = into or (lambda bench: "A")                 # where the stream lands
        self.flow_mult = flow_mult or (lambda bench: 1.0)       # surges
        self.extra = extra or (lambda bench, dt: None)          # glugs and other events
        self.noise_px, self.spike_p = noise_px, spike_p
        self.conf = conf or (lambda bench, name: 1.0)           # camera confidence
        self.cam_dt, self._next_cam = 1.0 / cam_hz, 0.0
        self.rng = random.Random(seed)
        self.flow = 0.0
        self.poured = 0.0
        self.flow_t: float | None = None                         # when liquid first arrived
        self.max_tilt = 0.0
        self.hist = {n: deque(maxlen=3) for n in self.columns}
        clock.on_step.append(self._step)
        self._sample()

    @property
    def tilt(self) -> float:
        return math.degrees((self.joint.position() - self.base) * self.direction)

    def _step(self, dt: float) -> None:
        tilt = self.tilt
        self.max_tilt = max(self.max_tilt, tilt)
        want = 0.0 if self.dry else self.k * max(0.0, tilt - self.onset)
        self.flow += (want - self.flow) * min(1.0, dt / self.tau)
        if self.flow > 0.01 and self.flow_t is None:
            self.flow_t = self.clock.t
        f = self.flow * self.flow_mult(self) * dt
        self.ml[self.into(self)] += f
        self.poured += f
        self.extra(self, dt)
        if self.clock.t >= self._next_cam:
            self._next_cam = self.clock.t + self.cam_dt
            self._sample()

    def _sample(self) -> None:
        from level import LevelSample
        for n in self.columns:
            px = self.ml[n] / ML_PER_PX + self.rng.gauss(0.0, self.noise_px)
            if self.spike_p and self.rng.random() < self.spike_p:
                px += self.rng.choice([-1, 1]) * 25.0                 # a bad frame (hand, glare)
            px = max(0.0, min(BOX_PX, px))
            self.hist[n].append(LevelSample(px * ML_PER_PX, px, self.conf(self, n), self.clock.t, "none"))

    def read(self, name):
        """Median of the last 3 camera readings, like LevelTracker.read()."""
        from level import LevelSample
        last = list(self.hist[name])
        ml = sorted(s.ml for s in last)[len(last) // 2]
        px = sorted(s.px for s in last)[len(last) // 2]
        return LevelSample(ml, px, min(s.conf for s in last), last[-1].ts, "none")

    def flow_ml_s(self, name, window_s=0.3):
        return 0.0


def pour(*, direction=1, target=0.9, max_tilt=85.0, start=0.0, stop_at=None, cfg=None, **bench_kw):
    """One pour into A, as pour_episode runs it. Returns (PourResult, bench, joint)."""
    clock = SimClock()
    joint = SimJoint(clock, start=math.radians(start))
    bench = SimBench(clock, joint, direction, **bench_kw)

    def on_tick(rec):
        if stop_at is not None and not rec.get("final") and rec["t"] >= stop_at:
            raise KeyboardInterrupt                                    # the operator presses q / STOP
    real, pour_guide.time = pour_guide.time, clock
    try:
        r = _pour(joint, bench, "A", direction, empty=[],
                  cfg=cfg or PourConfig(target_frac=target, max_tilt_deg=max_tilt),
                  clock=clock, verbose=False, on_tick=on_tick)
    finally:
        pour_guide.time = real
    return r, bench, joint


def safe(r, bench, joint, max_tilt=85.0, start=0.0):
    """Holds in every scenario."""
    assert bench.max_tilt <= max_tilt + 0.5, f"wrist went to {bench.max_tilt:.1f} deg, past max tilt {max_tilt}"
    assert abs(math.degrees(joint.position()) - start) < 1.0, "the wrist did not end upright"
    if r.result == "done":
        assert r.final_fill is not None and r.final_fill <= 1.05 + 1e-6, f"done but overfilled ({r.final_fill:.0%})"


def a_fill(bench, target):
    """A's level as a fraction of the target (in box terms)."""
    return bench.ml["A"] / (target * FULL_ML)


# ------------------------------------------------------------------ pours that should finish on target

@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("target", [0.3, 0.6, 0.9])
def test_normal_pour_reaches_the_target(direction, target):
    r, bench, joint = pour(direction=direction, target=target)
    safe(r, bench, joint)
    assert r.result == "done", r.reason
    assert 0.9 <= a_fill(bench, target) <= 1.05


def test_slows_down_as_it_fills():
    """The tilt comes back (pour slows) before the end: the rate schedule, not one fixed angle."""
    r, bench, joint = pour(target=0.9)
    safe(r, bench, joint)
    tilts = [x["tilt_deg"] for x in r.log]
    peak = max(tilts)
    assert r.result == "done" and tilts[-1] < peak - 2, "never eased off the tilt before stopping"
    actions = {x["action"] for x in r.log}
    assert {"tilt_more", "slow_down"} <= actions or {"tilt_more", "too_fast"} <= actions


@pytest.mark.parametrize("name,kw", [
    ("viscous (glycerin)", dict(k=0.015, tau=0.6)),
    ("thin and fast", dict(k=0.15, tau=0.1)),
    ("late onset (nearly empty vial)", dict(onset_deg=70.0)),
    ("early onset (full vial)", dict(onset_deg=15.0)),
])
def test_different_liquids_and_vials_still_reach_the_target(name, kw):
    r, bench, joint = pour(**kw)
    safe(r, bench, joint)
    assert r.result == "done", f"{name}: {r.reason}"
    assert 0.85 <= a_fill(bench, 0.9) <= 1.05, f"{name}: ended at {a_fill(bench, 0.9):.0%}"


def test_surge_does_not_overfill():
    r, bench, joint = pour(flow_mult=lambda b: 3.0 if b.flow_t and 1.0 < b.clock.t - b.flow_t < 1.4 else 1.0)
    safe(r, bench, joint)
    assert r.result in ("done", "aborted") and a_fill(bench, 0.9) <= 1.1


def test_noisy_camera_still_pours():
    r, bench, joint = pour(noise_px=2.0, seed=3)
    safe(r, bench, joint)
    assert r.result == "done", r.reason
    assert 0.85 <= a_fill(bench, 0.9) <= 1.08


def test_bad_frames_do_not_count_as_flow_or_stop_the_pour():
    r, bench, joint = pour(spike_p=0.05, seed=7)
    safe(r, bench, joint)
    assert r.result == "done", r.reason


def test_a_short_camera_dropout_holds_then_continues():
    """Hand in front of the camera for 0.5 s: hold the angle, then carry on."""
    def conf(b, name):
        return 0.1 if name == "A" and b.flow_t and 1.5 < b.clock.t - b.flow_t < 2.0 else 1.0
    r, bench, joint = pour(conf=conf)
    safe(r, bench, joint)
    assert r.result == "done", r.reason
    assert any(x["phase"] == "hold" for x in r.log)


def test_old_spill_in_the_neighbours_is_not_a_wrong_column():
    r, bench, joint = pour(start_ml={"x1": 0.8, "x2": 0.5, "B": 2.0})
    safe(r, bench, joint)
    assert r.result == "done", r.reason


def test_container_already_holding_some_liquid_tops_up():
    r, bench, joint = pour(start_ml={"A": 1.6})              # 40 % of the box already, target 90 %
    safe(r, bench, joint)
    assert r.result == "done" and 0.9 <= a_fill(bench, 0.9) <= 1.05


def test_already_at_the_target_pours_almost_nothing():
    r, bench, joint = pour(start_ml={"A": 0.9 * FULL_ML})
    safe(r, bench, joint)
    assert r.result == "done" and bench.poured < 0.1


# ------------------------------------------------------------------ pours that must stop

def test_dry_vial_stops_at_max_tilt():
    r, bench, joint = pour(dry=True)
    safe(r, bench, joint)
    assert r.result == "aborted" and "no flow at max tilt" in r.reason


def test_dry_vial_with_a_noisy_camera_still_stops():
    """Noise must not look like flow and keep the wrist tilted forever."""
    r, bench, joint = pour(dry=True, noise_px=2.0, spike_p=0.03, seed=11)
    safe(r, bench, joint)
    assert r.result == "aborted" and "no flow" in r.reason


@pytest.mark.parametrize("into,side", [("x2", "RIGHT"), ("x1", "LEFT"), ("B", "RIGHT")])
def test_stream_missing_the_container_stops_and_says_which_side(into, side):
    r, bench, joint = pour(into=lambda b: into if b.poured > 0.6 else "A")
    safe(r, bench, joint)
    assert r.result == "aborted"
    assert r.wrong_column == into and r.wrong_side == side.lower(), (r.reason, r.wrong_column, r.wrong_side)
    assert bench.ml[into] < 1.0, f"{bench.ml[into]:.2f} mL went into {into} before it stopped"


def test_sudden_glug_overfill_stops():
    def glug(b, dt):
        if not getattr(b, "_glugged", False) and b.ml["A"] > 0.7 * FULL_ML:
            b.ml["A"] += 1.0
            b._glugged = True
    r, bench, joint = pour(extra=glug)
    safe(r, bench, joint)
    assert r.result == "aborted" and ("overflow" in r.reason.lower() or "rim" in r.reason)


def test_camera_lost_for_good_stops():
    r, bench, joint = pour(conf=lambda b, name: 0.1 if name == "A" and b.flow_t and b.clock.t - b.flow_t > 1.0 else 1.0)
    safe(r, bench, joint)
    assert r.result == "aborted" and "camera lost" in r.reason


def test_operator_stop_tilts_back():
    r, bench, joint = pour(stop_at=3.0)
    safe(r, bench, joint)
    assert r.result == "aborted" and "stopped by operator" in r.reason


def test_long_stream_lag_is_caught_as_overfilled_after_stop():
    """If liquid keeps coming long after tilt-back, the result must not say done when it overfilled."""
    r, bench, joint = pour(tau=1.2, k=0.12)
    safe(r, bench, joint)
    if a_fill(bench, 0.9) > 1.05:
        assert r.result == "aborted" and "overfilled after stop" in r.reason


def test_timeout_stops():
    r, bench, joint = pour(k=0.002, tau=0.5, cfg=PourConfig(target_frac=0.9, timeout_s=10.0))
    safe(r, bench, joint)
    assert r.result == "aborted" and ("timed out" in r.reason or "no flow" in r.reason)


def test_wrist_near_its_limit_refuses_without_moving():
    r, bench, joint = pour(start=80.0)                          # only 10 deg left before +90
    assert r.result == "not_started" and "room" in r.reason
    assert bench.max_tilt == 0.0


def test_lower_max_tilt_is_respected():
    r, bench, joint = pour(max_tilt=50.0, onset_deg=45.0)
    safe(r, bench, joint, max_tilt=50.0)


# ------------------------------------------------------------------ a camera too slow to steer by

def test_slow_camera_does_not_overfill():
    """At 4 readings a second the fill rate can't be measured. The pour must still not run past the target."""
    r, bench, joint = pour(cam_hz=4.0)
    safe(r, bench, joint)
    assert a_fill(bench, 0.9) <= 1.1, f"slow camera: ended at {a_fill(bench, 0.9):.0%} of the target"


# ------------------------------------------------------------------ liquid only starts near max tilt
# What the real runs did (Tiger, 2026-10-25): J6 started at +7..+11 deg, so max tilt was 77-80 deg; the wrist ramped
# straight to it and stopped at once with "no flow at max tilt" - it never waited there for the first drop.

@pytest.mark.parametrize("onset", [70.0, 74.0, 77.0])
def test_onset_just_below_max_tilt_still_pours(onset):
    r, bench, joint = pour(start=8.0, onset_deg=onset)          # max tilt = 90 - 8 - 2 = 80 deg
    safe(r, bench, joint, max_tilt=80.0, start=8.0)
    assert r.result == "done", r.reason
    assert 0.9 <= a_fill(bench, 0.9) <= 1.05


def test_slow_first_drop_at_max_tilt_still_pours():
    """The first drop takes a while to come out (surface tension at the lip): wait for it at max tilt."""
    r, bench, joint = pour(start=8.0, onset_deg=77.0, tau=1.0)
    safe(r, bench, joint, max_tilt=80.0, start=8.0)
    assert r.result == "done", r.reason


def test_dry_vial_waits_at_max_tilt_then_stops():
    """A dry vial still stops, after dry_s at max tilt (not straight away, and not much later)."""
    cfg = PourConfig(target_frac=0.9)
    r, bench, joint = pour(dry=True, cfg=cfg)
    safe(r, bench, joint)
    assert r.result == "aborted" and "no flow at max tilt" in r.reason
    at_max = [rec["t"] for rec in r.log if rec["tilt_deg"] >= 85.0 - 1e-6]
    assert at_max and cfg.dry_s - 0.1 <= at_max[-1] - at_max[0] <= cfg.dry_s + 0.5


@pytest.mark.parametrize("onset", [45.0, 60.0])
def test_search_does_not_tilt_far_past_the_onset(onset):
    """A full, runny vial: the first rise must stop the search before the wrist is far past where liquid started."""
    r, bench, joint = pour(onset_deg=onset, k=0.12)
    safe(r, bench, joint)
    assert r.result == "done", r.reason
    first = next(rec for rec in r.log if rec["action"] != "tilt_more")
    assert first["tilt_deg"] - onset <= 6.0, f"still searching {first['tilt_deg'] - onset:.1f} deg past the onset"
