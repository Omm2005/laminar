from laminar.level import FakeLevelTracker


def test_reads_what_was_added():
    lt = FakeLevelTracker(noise_ml=0)
    lt.start(curve=[0.06, 0.0])
    lt.add(2.0)
    s = lt.read()
    assert abs(s.ml - 2.0) <= 0.06  # one pixel of quantisation
    assert s.px > 0 and s.conf == 1.0


def test_zero_makes_current_level_zero():
    lt = FakeLevelTracker(noise_ml=0)
    lt.add(3.0)
    lt.zero()
    lt.add(1.0)
    assert abs(lt.read().ml - 1.0) <= 0.06


def test_stuck_tracker_does_not_rise():
    lt = FakeLevelTracker(noise_ml=0)
    lt.stuck = True
    lt.add(2.0)
    assert lt.read().ml == 0.0
