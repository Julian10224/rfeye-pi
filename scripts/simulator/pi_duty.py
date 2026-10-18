"""Slow another process down by ``ratio``: run it 1/ratio of every short period.

    python pi_duty.py <pid> <ratio> [period_ms]

Suspends and resumes the whole process (NtSuspendProcess / NtResumeProcess)
on a busy-waited clock, so it gets 1/ratio of the wall time in slices of
period/ratio, and never stalls longer than one period. A job-object hard cap
does the same on average but freezes a process for ~0.6 s at a time, which a
real-time stream does not survive and a real Pi never does to it.

Exits when the target does.
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

pid, ratio = int(sys.argv[1]), float(sys.argv[2])
period = (float(sys.argv[3]) if len(sys.argv) > 3 else 10.0) / 1000.0
run = period / ratio

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
k32.OpenProcess.restype = wintypes.HANDLE
k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
h = k32.OpenProcess(0x0800 | 0x1000, False, pid)   # SUSPEND_RESUME | QUERY_LIMITED
if not h:
    sys.exit("cannot open %d" % pid)
code = wintypes.DWORD()
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)   # HIGH: never late
# ...and never on the target's cores, or its busy-wait starves the target.
k32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
k32.SetProcessAffinityMask(k32.GetCurrentProcess(), 1 << (os.cpu_count() - 2))
nxt = time.perf_counter()
try:
    while True:
        ntdll.NtResumeProcess(h)
        nxt += run
        while time.perf_counter() < nxt:
            pass
        ntdll.NtSuspendProcess(h)
        nxt += period - run
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        if code.value != 259:                     # STILL_ACTIVE
            break
        while time.perf_counter() < nxt:
            pass
        if time.perf_counter() - nxt > period:     # fell behind: do not burst
            nxt = time.perf_counter()
finally:
    ntdll.NtResumeProcess(h)
