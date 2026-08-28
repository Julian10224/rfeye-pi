#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit('usage: patch-radar-buzzer.py /path/to/app.py')

p = Path(sys.argv[1])
s = p.read_text()

# RF Eye hardware build now targets the TMB12A03 active buzzer. Force active
# mode even when an older persisted config still contains buzzer_passive=true.
needle = '        self.buzzer = GPIOBuzzer(\n'
if needle in s and 'TMB12A03 active buzzer' not in s:
    s = s.replace(
        needle,
        '        # TMB12A03 active buzzer: do not drive its internal oscillator with PWM.\n'
        '        self.cfg["buzzer_passive"] = False\n'
        '        self.cfg["buzzer_model"] = "TMB12A03"\n'
        + needle,
        1,
    )

# One-shot boot acknowledgement. Do not sound merely because the UI exists:
# wait until the RF backend reports LIVE, so the rhythm confirms functionality.
if 'self.ready_chime_done' not in s:
    init_needle = '        self.last_beep = 0.0\n'
    if init_needle not in s:
        raise SystemExit('ready chime init point missing')
    s = s.replace(init_needle, init_needle + '        self.ready_chime_done = False\n', 1)

start = s.find('    def _sound_logic(self, snap):\n')
end = s.find('    def _text(', start)
if start < 0 or end < 0:
    raise SystemExit('RF Eye sound logic insertion point missing')

new = '''    def _sound_logic(self, snap):
        if self.cfg.get("muted", False):
            self.buzzer.off()
            return

        # TMB12A03 is an active buzzer with one fixed pitch, so the startup
        # "jingle" is a distinct short-short-long rhythm. It is played exactly
        # once, only after a real SDR scan has reached LIVE state.
        if not self.ready_chime_done and snap.get("status") == "LIVE":
            self.ready_chime_done = True
            if self.cfg.get("startup_chime", True):
                self.buzzer.beep_pattern([(70, 55), (70, 60), (175, 0)])
                self.last_beep = time.time() + 0.25
            return

        peaks = snap["peaks"]
        if not peaks:
            return

        lv = max(float(p.get("level", 0.0)) for p in peaks)
        if lv < 0.15:
            return

        # TMB12A03 has one fixed internal tone, so make the LOW/MEDIUM/HIGH
        # zones deliberately different by rhythm rather than tiny pitch changes.
        # Each ON time is kept comfortably above the buzzer's response time.
        if lv > 0.72:
            # HIGH/red: urgent triple burst with very short gaps.
            on_ms = int(self.cfg.get("buzzer_red_ms", 105))
            gap_ms = int(self.cfg.get("buzzer_red_gap_ms", 45))
            pattern = [(on_ms, gap_ms), (on_ms, gap_ms), (on_ms, 0)]
            base_interval = 0.46
        elif lv > 0.43:
            # MEDIUM/yellow: unmistakable double beep.
            on_ms = int(self.cfg.get("buzzer_yellow_ms", 130))
            gap_ms = int(self.cfg.get("buzzer_yellow_gap_ms", 135))
            pattern = [(on_ms, gap_ms), (on_ms, 0)]
            base_interval = 0.95
        else:
            # LOW/green: one long, calm pulse with a large quiet gap.
            on_ms = int(self.cfg.get("buzzer_green_ms", 185))
            pattern = [(on_ms, 0)]
            base_interval = 1.85

        # Keep a little within-zone acceleration, but never enough to erase
        # the strong one/double/triple-beep distinction between the colours.
        if self.cfg.get("audio_mode", "adaptive") == "adaptive":
            if lv > 0.72:
                zone_n = max(0.0, min(1.0, (lv - 0.72) / 0.28))
                interval = max(0.40, base_interval - 0.06 * zone_n)
            elif lv > 0.43:
                zone_n = max(0.0, min(1.0, (lv - 0.43) / 0.29))
                interval = max(0.82, base_interval - 0.13 * zone_n)
            else:
                zone_n = max(0.0, min(1.0, (lv - 0.15) / 0.28))
                interval = max(1.45, base_interval - 0.40 * zone_n)
        else:
            interval = base_interval

        # Ensure a complete pattern always has time to finish before a new one
        # is started. beep_pattern itself is generation-safe, but this also makes
        # the audible rhythm much cleaner and easier to recognise.
        pattern_ms = sum(on + gap for on, gap in pattern)
        interval = max(interval, pattern_ms / 1000.0 + 0.08)

        now = time.time()
        if now - self.last_beep >= interval:
            self.buzzer.beep_pattern(pattern)
            self.last_beep = now

'''

s = s[:start] + new + s[end:]
compile(s, str(p), 'exec')
p.write_text(s)
