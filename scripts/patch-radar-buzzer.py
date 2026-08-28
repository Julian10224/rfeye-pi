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
        if lv < 0.15:
            return

        # TMB12A03 is an ACTIVE buzzer with its own ~2.3 kHz oscillator.
        # Frequency PWM makes it sound clipped/raspy. Different levels are
        # therefore encoded as complete pulse patterns instead of carrier pitch.
        if lv > 0.72:
            on_ms = int(self.cfg.get("buzzer_red_ms", 75))
            gap_ms = int(self.cfg.get("buzzer_red_gap_ms", 55))
            pattern = [(on_ms, gap_ms), (on_ms, gap_ms), (on_ms, 0)]
        elif lv > 0.43:
            on_ms = int(self.cfg.get("buzzer_yellow_ms", 85))
            gap_ms = int(self.cfg.get("buzzer_yellow_gap_ms", 70))
            pattern = [(on_ms, gap_ms), (on_ms, 0)]
        else:
            on_ms = int(self.cfg.get("buzzer_green_ms", 95))
            pattern = [(on_ms, 0)]

        n = max(0.0, min(1.0, (lv - 0.15) / 0.85))
        interval = max(0.42, 1.35 * (0.31 ** n))
        if self.cfg.get("audio_mode", "adaptive") != "adaptive":
            interval = 1.0

        now = time.time()
        if now - self.last_beep >= interval:
            self.buzzer.beep_pattern(pattern)
            self.last_beep = now

'''

s = s[:start] + new + s[end:]
compile(s, str(p), 'exec')
p.write_text(s)
