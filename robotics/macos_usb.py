"""macOS fixes for the candleLight (gs_usb) adapter. Call patch() before creating a RobStrideBus.

- gs_usb looks for libusb in system paths; point it at the copy bundled with libusb-package.
- gs_usb asks to detach a kernel driver, which macOS refuses without root. The CAN interface
  has no macOS driver attached, so report "not active" and skip the detach.
"""
import platform

_done = False


def patch():
    global _done
    if _done or platform.system() != "Darwin":
        return
    import libusb_package
    import usb.core
    from usb.backend import libusb1

    get_backend = libusb1.get_backend
    libusb1.get_backend = lambda find_library=None: get_backend(find_library=lambda _: libusb_package.get_library_path())
    usb.core.Device.is_kernel_driver_active = lambda self, interface: False
    _done = True
