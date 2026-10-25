"""The CAN receive thread survives USB read errors (it used to die silently: no feedback ever again)."""
import queue
import threading
import time

import robstride_can as R


class FlakyUsb:
    def __init__(self):
        self.calls = 0

    def recv(self, timeout):
        self.calls += 1
        if self.calls == 3:
            raise OSError("USB read failed")
        return None


def test_receive_thread_keeps_reading_after_a_usb_error():
    bus = R.RobStrideBus.__new__(R.RobStrideBus)       # no adapter: wire up the parts the thread uses
    bus.bus, bus.state, bus._rx = FlakyUsb(), {}, queue.Queue()
    bus._usb_lock, bus._running, bus.rx_errors, bus.last_rx_error = threading.Lock(), True, 0, None
    t = threading.Thread(target=bus._rx_loop, daemon=True)
    t.start()
    time.sleep(0.3)
    try:
        assert t.is_alive() and bus.rx_errors == 1 and bus.bus.calls > 10   # error counted, still reading
    finally:
        bus._running = False
        t.join(1.0)
