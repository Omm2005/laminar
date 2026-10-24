"""Run any robotics/ script on macOS: python mac_run.py detect_arm.py [args...]

Applies the macOS USB fixes from macos_usb.py, then runs the script unchanged.
"""
import runpy
import sys

import macos_usb

macos_usb.patch()

if len(sys.argv) < 2:
    sys.exit(__doc__)
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
