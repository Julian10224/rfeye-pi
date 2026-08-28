#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit('usage: patch-startup-splash.py /path/to/app.py')

app = Path(sys.argv[1])
s = app.read_text()

# Keep exactly one very early splash draw. Repainting four intermediate startup
# stages costs noticeable time on a Pi 3B+ and does not make initialization faster.
if 'def _startup_splash(' not in s:
    font_needle = '        self.font_xl = pygame.font.Font(None, 56)\n'
    if font_needle not in s:
        raise SystemExit('RF Eye startup splash font insertion point missing')
    s = s.replace(
        font_needle,
        font_needle + '        self.font_boot = pygame.font.Font(None, 82)\n\n'
        '        self._startup_splash(0.18, "STARTING")\n',
        1,
    )

    method_needle = '    def _make_beep(self, freq, ms, volume):\n'
    if method_needle not in s:
        raise SystemExit('RF Eye startup splash method insertion point missing')
    method = '''    def _startup_splash(self, progress, status):
        self.ui.fill((0, 0, 0))
        self._text("RF EYE", self.uw // 2, 315, self.font_boot, BLUE_BRIGHT, center=True)
        self._text(status, self.uw // 2, 382, self.font_s, (86, 126, 146), center=True)
        track = pygame.Rect(55, 445, 370, 14)
        pygame.draw.rect(self.ui, (18, 27, 34), track, border_radius=7)
        pygame.draw.rect(self.ui, (43, 67, 80), track, 1, border_radius=7)
        fill_w = max(8, int((track.width - 4) * max(0.0, min(1.0, progress))))
        pygame.draw.rect(self.ui, BLUE_BRIGHT, (track.x + 2, track.y + 2, fill_w, track.height - 4), border_radius=5)
        self._present_rotated()
        pygame.display.flip()
        pygame.event.pump()

'''
    s = s.replace(method_needle, method + method_needle, 1)

compile(s, str(app), 'exec')
app.write_text(s)
