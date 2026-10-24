"""Minimal RobStride CAN driver for the reBot Arm B601-RS over a candleLight (gs_usb) adapter on Windows.

Protocol: 29-bit extended IDs, 1 Mbps.  ID = (comm_type << 24) | (data2 << 8) | target_id
"""
import math
import os
import queue
import struct
import threading
import time

import libusb_package

# python-can's gs_usb backend uses pyusb's default backend lookup; make the bundled libusb DLL findable.
os.environ["PATH"] = os.path.dirname(libusb_package.get_library_path()) + os.pathsep + os.environ["PATH"]

import can  # noqa: E402

HOST_ID = 0xFD

# comm types
T_GET_ID = 0
T_MIT = 1
T_FEEDBACK = 2
T_ENABLE = 3
T_STOP = 4
T_READ_PARAM = 17
T_WRITE_PARAM = 18

# parameters
P_RUN_MODE = 0x7005
P_MECH_POS = 0x7019
P_VBUS = 0x701C

P_MIN, P_MAX = -4 * math.pi, 4 * math.pi
# model: (vel range, torque range, kp max, kd max)
MODELS = {
    "rs-00": (33.0, 14.0, 500.0, 5.0),
    "rs-06": (50.0, 36.0, 5000.0, 100.0),
}

# reBot Arm B601-RS joint map (motor_id -> model, MIT kp, kd, lower/upper limit in degrees)
# Gains from Seeed's rebotarm_rs.yaml, limits from the vendor URDF / spec sheet.
JOINTS = {
    1: ("rs-06", 50.0, 3.0, -150.0, 150.0),
    2: ("rs-06", 150.0, 10.0, 0.0, 180.0),
    3: ("rs-06", 150.0, 10.0, 0.0, 180.0),
    4: ("rs-00", 50.0, 5.0, -90.0, 90.0),
    5: ("rs-00", 50.0, 4.0, -90.0, 90.0),
    6: ("rs-00", 50.0, 4.0, -180.0, 180.0),
    7: ("rs-00", 50.0, 4.0, 0.0, 345.0),  # gripper
}


def _u16(x, lo, hi):
    x = min(max(x, lo), hi)
    return int(round((x - lo) / (hi - lo) * 65535))


def _f(u, lo, hi):
    return u / 65535 * (hi - lo) + lo


class RobStrideBus:
    def __init__(self, bitrate=1_000_000):
        self.bus = can.Bus(interface="gs_usb", channel=0, index=0, bitrate=bitrate)
        self.state = {}  # motor_id -> dict(pos, vel, torque, temp, fault, mode, t)
        # gs_usb recv() costs ~14 ms when nothing is waiting on Windows, so receive on a background thread
        self._rx = queue.Queue()
        self._running = True
        self._thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._thread.start()

    def _rx_loop(self):
        while self._running:
            try:
                msg = self.bus.recv(0.05)
            except Exception:
                if not self._running:
                    return
                raise
            if msg is not None:
                r = self._handle(msg)
                if r and r[0] != T_FEEDBACK:
                    self._rx.put(r)

    def close(self):
        self._running = False
        self._thread.join(timeout=1.0)
        self.bus.shutdown()

    def _send(self, comm_type, data2, motor_id, data=b"\x00" * 8):
        arb = (comm_type << 24) | ((data2 & 0xFFFF) << 8) | motor_id
        self.bus.send(can.Message(arbitration_id=arb, data=data, is_extended_id=True))

    def _handle(self, msg):
        """Decode one frame; returns (comm_type, motor_id, msg)."""
        if not msg.is_extended_id:
            return None
        comm_type = (msg.arbitration_id >> 24) & 0x1F
        motor_id = (msg.arbitration_id >> 8) & 0xFF  # replies carry the motor ID in bits 8-15
        if comm_type == T_FEEDBACK and len(msg.data) == 8 and motor_id in JOINTS:
            vmax, tmax, _, _ = MODELS[JOINTS[motor_id][0]]
            p, v, t, temp = struct.unpack(">HHHH", msg.data)
            self.state[motor_id] = dict(
                pos=_f(p, P_MIN, P_MAX), vel=_f(v, -vmax, vmax), torque=_f(t, -tmax, tmax),
                temp=temp / 10.0, fault=(msg.arbitration_id >> 16) & 0x3F, mode=(msg.arbitration_id >> 22) & 0x3,
                t=time.time(),
            )
        return comm_type, motor_id, msg

    def drain(self, timeout=0.0):
        """Discard queued replies (feedback is already applied to self.state by the receive thread)."""
        if timeout:
            time.sleep(timeout)
        while True:
            try:
                self._rx.get_nowait()
            except queue.Empty:
                return

    def _wait_for(self, comm_type, motor_id, timeout):
        end = time.time() + timeout
        while True:
            try:
                r = self._rx.get(timeout=max(0.0, end - time.time()))
            except queue.Empty:
                return None
            if r[0] == comm_type and r[1] == motor_id:
                return r[2]

    def ping(self, motor_id, timeout=0.05):
        self._send(T_GET_ID, HOST_ID, motor_id)
        return self._wait_for(T_GET_ID, motor_id, timeout) is not None

    def read_param_f32(self, motor_id, index, timeout=0.05):
        self._send(T_READ_PARAM, HOST_ID, motor_id, struct.pack("<HHf", index, 0, 0.0))
        msg = self._wait_for(T_READ_PARAM, motor_id, timeout)
        return None if msg is None else struct.unpack("<f", bytes(msg.data[4:8]))[0]

    def write_param_u8(self, motor_id, index, value):
        self._send(T_WRITE_PARAM, HOST_ID, motor_id, struct.pack("<HHB3x", index, 0, value))

    def enable(self, motor_id):
        self._send(T_ENABLE, HOST_ID, motor_id)

    def disable(self, motor_id, clear_fault=False):
        self._send(T_STOP, HOST_ID, motor_id, bytes([1 if clear_fault else 0]) + b"\x00" * 7)

    def mit(self, motor_id, pos, kp, kd, vel=0.0, torque=0.0):
        vmax, tmax, kpmax, kdmax = MODELS[JOINTS[motor_id][0]]
        data = struct.pack(">HHHH", _u16(pos, P_MIN, P_MAX), _u16(vel, -vmax, vmax),
                           _u16(kp, 0, kpmax), _u16(kd, 0, kdmax))
        self._send(T_MIT, _u16(torque, -tmax, tmax), motor_id, data)
