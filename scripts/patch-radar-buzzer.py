#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit('usage: patch-radar-buzzer.py /path/to/app.py')

p = Path(sys.argv[1])
s = p.read_text()

start = s.find('    def _sound_logic(self, snap):\n')
end = s.find('    def _text(', start)
if start < 0 or end < 0:
    raise SystemExit('RF Eye sound logic insertion point missing')

new = '''    def _sound_logic(self, snap):
        if self.cfg.get("muted", False):
            self.buzzer.off()
            return
        peaks = snap["peaks"]
        if not peaks:
            return

        lv = max(float(p.get("level", 0.0)) for p in peaks)
        # Match the visible LOW/MEDIUM/HIGH zones in the UI.
        if lv < 0.15:
            return

        if lv > 0.72:
            freq = int(self.cfg.get("buzzer_red_hz", 3200))
        elif lv > 0.43:
            freq = int(self.cfg.get("buzzer_yellow_hz", 2700))
        else:
            freq = int(self.cfg.get("buzzer_green_hz", 2200))

        # Same Geiger/radar-style timing idea as the ESP32 reference:
        # slow isolated clicks when weak, rapidly accelerating when strong.
        n = max(0.0, min(1.0, (lv - 0.15) / 0.85))
        interval = max(0.030, 1.000 * (0.035 ** n))
        pulse_ms = max(12, min(30, int(interval * 500)))

        if self.cfg.get("audio_mode", "adaptive") != "adaptive":
            interval = 0.75
            pulse_ms = 25

        now = time.time()
        if now - self.last_beep >= interval:
            self.buzzer.beep(
                frequency=freq,
                duration_ms=pulse_ms,
                duty=int(self.cfg.get("buzzer_duty", 50)),
            )
            self.last_beep = now

'''

s = s[:start] + new + s[end:]
compile(s, str(p), 'exec')
p.write_text(s)
