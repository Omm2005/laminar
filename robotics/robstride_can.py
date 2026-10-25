"""Minimal RobStride CAN driver for the reBot Arm B601-RS over a candleLight (gs_usb) adapter on Windows.

Protocol: 29-bit extended IDs, 1 Mbps.  ID = (comm_type << 24) | (data2 << 8) | target_id
"""
import math
import os
import queue
import struct
import sys
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


def fine_timer():
    """Windows rounds time.sleep() up to its ~15.6 ms timer tick (Python < 3.11), so a 100 Hz loop runs at
    ~55 Hz with jitter. Ask for 1 ms timer resolution for this process (released when it exits)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass



class _Batch:
    """Hold the USB lock, flagging the receive thread to step aside while we wait for it."""

    def __init__(self, bus):
        self.b = bus

    def __enter__(self):
        self.b._tx_waiting += 1
        self.b._usb_lock.acquire()
        return self

    def __exit__(self, *exc):
        self.b._usb_lock.release()
        self.b._tx_waiting -= 1
        return False

class RobStrideBus:
    def __init__(self, bitrate=1_000_000):
        self.bus = can.Bus(interface="gs_usb", channel=0, index=0, bitrate=bitrate)
        self.state = {}  # motor_id -> dict(pos, vel, torque, temp, fault, mode, t)
        # gs_usb recv() costs ~14 ms when nothing is waiting on Windows, so receive on a background thread.
        # Reads and writes never overlap: concurrent USB transfers coincided with the adapter wedging
        # (USB I/O error, then no TX at all until replugged).
        # re-entrant, so a whole round of commands can go out under one hold (send_batch), and a sender waiting for
        # it makes the receive thread step aside: per-frame lock races made a 7-motor round take up to ~48 ms
        self._usb_lock = threading.RLock()
        self._tx_waiting = 0
        self._rx = queue.Queue()
        self.rx_errors, self.last_rx_error = 0, None  # USB read errors survived by the receive thread
        self._running = True
        self._thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._thread.start()

    def _rx_loop(self):
        while self._running:
            if self._tx_waiting:
                time.sleep(0.0002)
                continue
            try:
                with self._usb_lock:
                    msg = self.bus.recv(0.001)
            except Exception as e:
                if not self._running:
                    return
                # Never let a USB read error end this thread: it used to, silently, and then no feedback ever
                # arrived while commands still went out, so the caller saw "lost feedback" and disabled motors
                # that were holding the arm up (2026-10-24: the arm dropped). Count it, back off, keep reading.
                self.rx_errors += 1
                self.last_rx_error = repr(e)
                if self.rx_errors in (1, 10, 100) or self.rx_errors % 1000 == 0:
                    print(f"[robstride_can] CAN read error #{self.rx_errors}: {e!r} (still reading)",
                          file=sys.stderr, flush=True)
                time.sleep(0.01)
                continue
            if msg is not None:
                r = self._handle(msg)
                if r and r[0] != T_FEEDBACK:
                    self._rx.put(r)
            else:
                time.sleep(0.0005)  # let a waiting sender take the lock

    def close(self):
        self._running = False
        self._thread.join(timeout=1.0)
        self.bus.shutdown()

    def send_batch(self):
        """with bus.send_batch(): ... sends every command inside under one hold of the USB lock."""
        return _Batch(self)

    def _send(self, comm_type, data2, motor_id, data=b"\x00" * 8):
        arb = (comm_type << 24) | ((data2 & 0xFFFF) << 8) | motor_id
        with _Batch(self):
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


def boost_process() -> None:
    """Windows: high priority and no power throttling (EcoQoS) for this process only, so the 100 Hz loop is not
    parked for hundreds of ms while the laptop saves power."""
    if sys.platform != "win32":
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    proc = k32.GetCurrentProcess()
    k32.SetPriorityClass(proc, 0x80)  # HIGH_PRIORITY_CLASS

    class Throttle(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]
    t = Throttle(1, 0x1 | 0x4, 0)  # execution speed + timer resolution throttling: off
    k32.SetProcessInformation(proc, 4, ctypes.byref(t), ctypes.sizeof(t))  # ProcessPowerThrottling
    ctypes.windll.winmm.timeBeginPeriod(1)
