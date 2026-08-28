#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit('usage: patch-startup-splash.py /path/to/app.py')

app = Path(sys.argv[1])
s = app.read_text()

# Add the RF Eye in-app startup splash when the source does not already contain it.
if 'def _startup_splash(' not in s:
    font_needle = '        self.font_xl = pygame.font.Font(None, 56)\n'
    if font_needle not in s:
        raise SystemExit('RF Eye startup splash font insertion point missing')
    s = s.replace(
        font_needle,
        font_needle + '        self.font_boot = pygame.font.Font(None, 82)\n\n'
        '        self._startup_splash(0.12, "INITIALIZING")\n',
        1,
    )

    backend_needle = '        self.backend = SDRBackend(cfg)\n        self.backend.start()\n'
    if backend_needle not in s:
        raise SystemExit('RF Eye startup splash backend insertion point missing')
    s = s.replace(
        backend_needle,
        '        self._startup_splash(0.42, "LOADING INTERFACE")\n'
        '        self.backend = SDRBackend(cfg)\n'
        '        self._startup_splash(0.62, "STARTING SDR")\n'
        '        self.backend.start()\n'
        '        self._startup_splash(0.86, "CHECKING HARDWARE")\n',
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

# Replace the old two-tone/long-pulse audio with short, clean radar pips.
# Thresholds intentionally match the LOW/MEDIUM/HIGH green/yellow/red UI states.
if '# RF_EYE_RADAR_TONES_V1' not in s:
    old = '''    def _sound_logic(self, snap):
        if self.cfg.get("muted", False):
            return
        peaks = snap["peaks"]
        if not peaks:
            return
        lv = max(float(p.get("level", 0.0)) for p in peaks)
        if lv < 0.28:
            return
        now = time.time()
        interval = max(0.42, 3.3 - 2.7 * lv) if self.cfg.get("audio_mode", "adaptive") == "adaptive" else 2.5
        if now - self.last_beep >= interval:
            freq = (
                int(self.cfg.get("buzzer_high_hz", 1500))
                if lv > 0.72
                else int(self.cfg.get("buzzer_low_hz", 900))
            )
            self.buzzer.beep(
                frequency=freq,
                duration_ms=int(self.cfg.get("buzzer_duration_ms", 85)),
            )
            self.last_beep = now
'''
    new = '''    def _sound_logic(self, snap):
        # RF_EYE_RADAR_TONES_V1
        if self.cfg.get("muted", False):
            return
        peaks = snap["peaks"]
        if not peaks:
            return

        lv = max(float(p.get("level", 0.0)) for p in peaks)
        if lv <= 0.15:
            return

        # Match the visible RF Eye status colours:
        # green/LOW, yellow/MEDIUM and red/HIGH each have their own pitch.
        if lv > 0.72:
            frequency = int(self.cfg.get("buzzer_red_hz", 1450))
            duration_ms = int(self.cfg.get("buzzer_red_ms", 58))
            adaptive_interval = 0.48
        elif lv > 0.43:
            frequency = int(self.cfg.get("buzzer_yellow_hz", 1050))
            duration_ms = int(self.cfg.get("buzzer_yellow_ms", 52))
            adaptive_interval = 0.90
        else:
            frequency = int(self.cfg.get("buzzer_green_hz", 720))
            duration_ms = int(self.cfg.get("buzzer_green_ms", 48))
            adaptive_interval = 1.55

        interval = adaptive_interval if self.cfg.get("audio_mode", "adaptive") == "adaptive" else 1.25
        now = time.time()
        if now - self.last_beep >= interval:
            # Force the previous PWM pulse fully off before starting a new pip.
            # Short 32% duty pulses sound cleaner on a small passive piezo than
            # the previous long 50% square-wave beeps.
            self.buzzer.off()
            self.buzzer.beep(
                frequency=frequency,
                duration_ms=duration_ms,
                duty=32,
            )
            self.last_beep = now
'''
    if old not in s:
        raise SystemExit('RF Eye sound logic insertion point missing')
    s = s.replace(old, new, 1)

compile(s, str(app), 'exec')
app.write_text(s)
