"""Turn the calling process into an emulated Raspberry Pi 3 B+.

    import pi_mode; pi_mode.become_pi(ratio=20, cores=1)

Affinity to ``cores`` logical CPUs, then a controller process that lets it run
1/ratio of the time (see pi_duty.py).
"""
import atexit
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))


def become_pi(ratio, cores=1, period_ms=10.0):
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    mask = 0
    for i in range(int(cores)):
        mask |= 1 << (2 * i + 2)        # first threads of physical cores 1..n
    k32.SetProcessAffinityMask(k32.GetCurrentProcess(), mask)
    ctl = subprocess.Popen([sys.executable, os.path.join(HERE, "pi_duty.py"),
                            str(os.getpid()), str(float(ratio)), str(period_ms)])
    atexit.register(ctl.kill)
    return ctl
