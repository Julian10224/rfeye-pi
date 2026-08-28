#!/usr/bin/env python3
from pathlib import Path
import sys
from PIL import Image, ImageDraw, ImageFont

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('.')
out.mkdir(parents=True, exist_ok=True)
blue = (28, 190, 255)
dim = (86, 126, 146)

# RF Eye is logically 480x800 portrait but the HDMI framebuffer is 800x480.
# Draw the artwork in portrait coordinates and rotate it clockwise into the
# framebuffer, matching app.py's configured "cw" presentation.
logical = Image.new('RGB', (480, 800), (0, 0, 0))
draw = ImageDraw.Draw(logical)
font_candidates = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
]
font_path = next((p for p in font_candidates if Path(p).exists()), None)
if font_path:
    title = ImageFont.truetype(font_path, 72)
    subtitle = ImageFont.truetype(font_path, 18)
else:
    title = ImageFont.load_default()
    subtitle = title

def centered(text, y, font, fill):
    box = draw.textbbox((0, 0), text, font=font)
    x = (480 - (box[2] - box[0])) // 2
    draw.text((x, y), text, font=font, fill=fill)

centered('RF EYE', 280, title, blue)
centered('STARTING SYSTEM', 382, subtitle, dim)
logical.rotate(-90, expand=True).save(out / 'splash.png')

# A horizontal portrait progress bar must be vertical in the raw 800x480
# framebuffer. The physical portrait mounting rotates it back horizontally.
box = Image.new('RGBA', (14, 370), (22, 31, 38, 255))
box_draw = ImageDraw.Draw(box)
box_draw.rectangle((0, 0, 13, 369), outline=(43, 67, 80, 255), width=2)
box.save(out / 'progress_box.png')

bar = Image.new('RGBA', (6, 362), (*blue, 255))
bar.save(out / 'progress_bar.png')
