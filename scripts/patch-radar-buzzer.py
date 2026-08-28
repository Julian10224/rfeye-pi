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

        # Keep every tone close to the common ~2.7 kHz piezo resonance.
        # Large jumps away from resonance sound thin/harsh on small piezo discs.
        if lv > 0.72:
            freq = int(self.cfg.get("buzzer_red_hz", 2925))
            pulse_ms = int(self.cfg.get("buzzer_red_ms", 34))
        elif lv > 0.43:
            freq = int(self.cfg.get("buzzer_yellow_hz", 2700))
            pulse_ms = int(self.cfg.get("buzzer_yellow_ms", 39))
        else:
            freq = int(self.cfg.get("buzzer_green_hz", 2475))
            pulse_ms = int(self.cfg.get("buzzer_green_ms", 44))

        # Radar/Geiger cadence, but never so fast that a new pulse starts before
        # the previous tone has cleanly ended. This preserves an audible gap.
        n = max(0.0, min(1.0, (lv - 0.15) / 0.85))
        interval = max(0.085, 1.050 * (0.080 ** n))
        if self.cfg.get("audio_mode", "adaptive") != "adaptive":
            interval = 0.75

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
