#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit('usage: patch-debug-fix.py /path/to/app.py')

p = Path(sys.argv[1])
s = p.read_text()

# Keep settings touch hitboxes exactly aligned with the compact 10-row layout.
s = s.replace(
    '''        elif self.page == "settings":\n            if y < 90:\n                self.page = "main"\n                return\n\n            top = 116\n            rh = 66\n''',
    '''        elif self.page == "settings":\n            if y < 90:\n                self.page = "main"\n                return\n\n            top = 104\n            rh = 58\n''',
    1,
)

# The debug page uses right-aligned values. Older App._text only accepted center,
# which raised TypeError and made the page appear black/crash the draw loop.
old = '''    def _text(self, txt, x, y, font, color, center=False):\n        s = font.render(str(txt), True, color)\n        r = s.get_rect()\n        if center:\n            r.center = (int(x), int(y))\n        else:\n            r.topleft = (int(x), int(y))\n        self.ui.blit(s, r)\n'''
new = '''    def _text(self, txt, x, y, font, color, center=False, right=False):\n        s = font.render(str(txt), True, color)\n        r = s.get_rect()\n        if center:\n            r.center = (int(x), int(y))\n        elif right:\n            r.topright = (int(x), int(y))\n        else:\n            r.topleft = (int(x), int(y))\n        self.ui.blit(s, r)\n'''
if old in s:
    s = s.replace(old, new, 1)
elif 'center=False, right=False' not in s:
    raise SystemExit('text helper patch point missing')

compile(s, str(p), 'exec')
p.write_text(s)
print('RF Eye debug touch/display fix installed:', p)
