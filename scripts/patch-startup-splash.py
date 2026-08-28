#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit('usage: patch-startup-splash.py /path/to/app.py')

app = Path(sys.argv[1])
s = app.read_text()

# Keep the latest main behavior: one very early splash draw only. The inserted
# splash uses relative geometry so it stays proportional on both the standard
# 480x800 canvas and the CUQI 320x480 portrait canvas.
if 'def _startup_splash(' not in s:
    font_needle = '        self.font_xl = pygame.font.Font(None, 56)\n'
    if font_needle not in s:
        raise SystemExit('RF Eye startup splash font insertion point missing')
    s = s.replace(
        font_needle,
        font_needle + '        self.font_boot = pygame.font.Font(None, 54 if self.uw <= 320 else 82)\n\n'
        '        self._startup_splash(0.18, "STARTING")\n',
        1,
    )

    method_needle = '    def _make_beep(self, freq, ms, volume):\n'
    if method_needle not in s:
        raise SystemExit('RF Eye startup splash method insertion point missing')
    method = '''    def _startup_splash(self, progress, status):
        self.ui.fill((0, 0, 0))
        cx = self.uw // 2
        title_y = int(self.uh * 0.394)
        status_y = int(self.uh * 0.478)
        track_w = max(180, int(self.uw * 0.77))
        track_h = max(10, int(self.uh * 0.0175))
        track_x = (self.uw - track_w) // 2
        track_y = int(self.uh * 0.556)
        self._text("RF EYE", cx, title_y, self.font_boot, BLUE_BRIGHT, center=True)
        self._text(status, cx, status_y, self.font_s, (86, 126, 146), center=True)
        track = pygame.Rect(track_x, track_y, track_w, track_h)
        radius = max(4, track_h // 2)
        pygame.draw.rect(self.ui, (18, 27, 34), track, border_radius=radius)
        pygame.draw.rect(self.ui, (43, 67, 80), track, 1, border_radius=radius)
        fill_w = max(6, int((track.width - 4) * max(0.0, min(1.0, progress))))
        pygame.draw.rect(
            self.ui,
            BLUE_BRIGHT,
            (track.x + 2, track.y + 2, fill_w, max(4, track.height - 4)),
            border_radius=max(2, radius - 2),
        )
        self._present_rotated()
        pygame.display.flip()
        pygame.event.pump()

'''
    s = s.replace(method_needle, method + method_needle, 1)

compile(s, str(app), 'exec')
app.write_text(s)
