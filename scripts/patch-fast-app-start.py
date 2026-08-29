#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit('usage: patch-fast-app-start.py /path/to/app.py')

app = Path(sys.argv[1])
s = app.read_text()

# Keep Pygame on the earliest possible path, but defer NumPy/SDR imports until
# after the display is available. On the Pi 3 this removes roughly a second of
# warm startup time and, more importantly, lets Pygame import in parallel with
# Labwc during a cold boot.
s = s.replace(
    'import numpy as np\nimport pygame\n',
    'print(f"RFEYE_BOOT python-entry {time.monotonic():.3f}", flush=True)\n'
    'import pygame\n'
    'print(f"RFEYE_BOOT pygame-imported {time.monotonic():.3f}", flush=True)\n',
    1,
)
s = s.replace('from sdr_backend import SDRBackend\n', '', 1)

# Wait for Wayland inside Python, after the expensive Pygame import has already
# happened. The previous systemd shell loop waited first and imported second.
needle = '        pygame.display.init()\n'
if needle in s and 'RFEYE_BOOT wayland-ready' not in s:
    s = s.replace(
        needle,
        '        if os.getenv("WAYLAND_DISPLAY"):\n'
        '            sock = Path(os.getenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / os.getenv("WAYLAND_DISPLAY")\n'
        '            while not sock.exists():\n'
        '                time.sleep(0.05)\n'
        '        print(f"RFEYE_BOOT wayland-ready {time.monotonic():.3f}", flush=True)\n'
        + needle,
        1,
    )

needle = '        self.screen = pygame.display.set_mode((self.pw, self.ph), flags)\n'
if needle in s and 'RFEYE_BOOT display-ready' not in s:
    s = s.replace(
        needle,
        needle + '        print(f"RFEYE_BOOT display-ready {time.monotonic():.3f}", flush=True)\n',
        1,
    )

needle = '        self.backend = SDRBackend(cfg)\n        self.backend.start()\n'
if needle in s and 'from sdr_backend import SDRBackend' not in s:
    s = s.replace(
        needle,
        '        from sdr_backend import SDRBackend\n'
        + needle
        + '        print(f"RFEYE_BOOT backend-started {time.monotonic():.3f}", flush=True)\n',
        1,
    )

# The legacy tone helper is not used by the TMB12A03 active buzzer. Preserve it
# but import NumPy lazily if it is ever called.
needle = '            sr = 22050\n            t = np.linspace('
if needle in s and '            import numpy as np\n            sr = 22050' not in s:
    s = s.replace(
        '            sr = 22050\n',
        '            import numpy as np\n            sr = 22050\n',
        1,
    )

s = s.replace(
    'pmax = max(float(np.max(p)), pmin + 45)',
    'pmax = max(max(float(v) for v in p), pmin + 45)',
)

compile(s, str(app), 'exec')
app.write_text(s)
