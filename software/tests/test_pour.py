import socket
import time

import pytest

from laminar.pour import PourController, PourFrame, PourState, UdpPourFeed, send_frame


def frame(ml=0.0, flow=0.0, pouring=None, color="blue", conf=1.0, ts=0.0):
    pouring = flow > 0.1 if pouring is None else pouring
    return PourFrame(ts=ts, ml=ml, flow_ml_s=flow, pouring=pouring, color=color, color_conf=0.9, conf=conf)


def test_json_round_trip_and_unknown_fields_ignored():
    f = PourFrame.from_json('{"ts": 1.5, "ml": 0.4, "flow_ml_s": 0.8, "pouring": true, '
                            '"color": "blue", "color_conf": 0.9, "conf": 0.95, "extra": 1}')
    assert f.ml == 0.4 and f.pouring and f.color == "blue"
    assert PourFrame.from_json(f.to_json()) == f


def test_json_missing_required_field_rejected():
    with pytest.raises(ValueError, match="ml"):
        PourFrame.from_json('{"ts": 1.0}')


def test_no_flow_tilts_more():
    c = PourController(2.0, "blue")
    cmd = c.step(frame(), now=0.0, dt=0.1)
    assert cmd.action == "tilt_more" and cmd.tilt > 0 and cmd.state == PourState.SEARCHING


def test_too_fast_slows_down():
    c = PourController(2.0, "blue")
    c.tilt = 1.2
    cmd = c.step(frame(ml=0.5, flow=1.6), now=0.0, dt=0.1)
    assert cmd.action == "slow_down" and cmd.tilt < 1.2 and "too fast" in cmd.reason


def test_near_target_slows_down():
    c = PourController(2.0, "blue")
    c.tilt = 1.2
    cmd = c.step(frame(ml=1.6, flow=0.9), now=0.0, dt=0.1)
    assert cmd.state == PourState.SLOWING and cmd.action == "slow_down" and cmd.tilt < 1.2


def test_surge_backs_off_hard():
    c = PourController(2.0, "blue")
    c.tilt = 1.2
    cmd = c.step(frame(ml=0.5, flow=3.0), now=0.0, dt=0.1)
    assert cmd.action == "slow_down" and "surge" in cmd.reason and cmd.tilt == pytest.approx(1.0)


def test_target_reached_stops():
    c = PourController(2.0, "blue")
    cmd = c.step(frame(ml=1.95, flow=0.3), now=0.0, dt=0.1)
    assert cmd.action == "stop" and c.state == PourState.DONE and not c.active


def test_flow_stopped_mid_pour_tilts_more():
    c = PourController(2.0, "blue")
    c.tilt = 1.1
    c.step(frame(ml=0.5, flow=1.0), now=0.0, dt=0.1)
    before = c.tilt
    cmd = c.step(frame(ml=0.6, flow=0.0, ts=0.1), now=0.1, dt=0.1)
    assert cmd.action == "tilt_more" and "stopped" in cmd.reason and cmd.tilt > before


def test_dry_at_max_tilt_aborts():
    c = PourController(2.0, "blue")
    c.tilt = c.r.max_tilt
    for i in range(20):
        c.step(frame(ts=i * 0.1), now=i * 0.1, dt=0.1)
    assert c.state == PourState.ABORTED and "no flow at max tilt" in c.reason


def test_wrong_color_aborts_after_three_frames():
    c = PourController(2.0, "blue")
    assert c.step(frame(ml=0.2, flow=0.8, color="red"), 0.0, 0.033).action != "abort"
    assert c.step(frame(ml=0.2, flow=0.8, color="red"), 0.0, 0.033).action != "abort"
    cmd = c.step(frame(ml=0.2, flow=0.8, color="red"), 0.0, 0.033)
    assert cmd.action == "abort" and "wrong liquid" in cmd.reason


def test_one_wrong_color_frame_is_ignored():
    c = PourController(2.0, "blue")
    c.step(frame(ml=0.2, flow=0.8, color="red"), 0.0, 0.033)
    c.step(frame(ml=0.2, flow=0.8, color="blue"), 0.0, 0.033)
    c.step(frame(ml=0.2, flow=0.8, color="red"), 0.0, 0.033)
    assert c.active


def test_stale_or_low_conf_holds_then_aborts():
    c = PourController(2.0, "blue")
    c.tilt = 1.0
    cmd = c.step(frame(ml=0.5, flow=1.0, ts=0.0), now=1.0, dt=0.1)  # frame 1 s old
    assert cmd.action == "hold" and cmd.tilt == 1.0
    for _ in range(4):
        cmd = c.step(frame(ml=0.5, flow=1.0, conf=0.1, ts=1.0), now=1.0, dt=0.1)
    assert c.state == PourState.ABORTED and "camera unusable" in c.reason


def test_udp_feed_returns_newest_frame_and_skips_garbage():
    feed = UdpPourFeed(port=0)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send_frame(tx, frame(ml=0.1, ts=1.0), port=feed.port)
    tx.sendto(b"not json", ("127.0.0.1", feed.port))
    send_frame(tx, {"ts": 2.0, "ml": 0.7, "flow_ml_s": 0.9, "pouring": True, "color": "blue"}, port=feed.port)
    time.sleep(0.05)
    f = feed.latest()
    feed.close()
    tx.close()
    assert f.ml == 0.7 and feed.bad == 1
