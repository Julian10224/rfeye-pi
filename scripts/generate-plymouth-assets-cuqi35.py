#!/usr/bin/env python3
from pathlib import Path
import sys
from PIL import Image, ImageDraw, ImageFont

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('.')
out.mkdir(parents=True, exist_ok=True)
blue = (28, 190, 255)
dim = (86, 126, 146)

# Native RF Eye portrait canvas for the CUQI panel is 320x480. Rotate once
# clockwise into the display controller's native 480x320 framebuffer.
logical = Image.new('RGB', (320, 480), (0, 0, 0))
draw = ImageDraw.Draw(logical)
font_candidates = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
]
font_path = next((p for p in font_candidates if Path(p).exists()), None)
if font_path:
    title = ImageFont.truetype(font_path, 48)
    subtitle = ImageFont.truetype(font_path, 14)
else:
    title = ImageFont.load_default()
    subtitle = title

def centered(text, y, font, fill):
    box = draw.textbbox((0, 0), text, font=font)
    x = (320 - (box[2] - box[0])) // 2
    draw.text((x, y), text, font=font, fill=fill)

centered('RF EYE', 168, title, blue)
centered('STARTING SYSTEM', 234, subtitle, dim)
logical.rotate(-90, expand=True).save(out / 'splash.png')

# Plymouth animates height in raw framebuffer coordinates. With the physical
# screen mounted vertically this becomes a horizontal progress bar.
box = Image.new('RGBA', (12, 226), (22, 31, 38, 255))
box_draw = ImageDraw.Draw(box)
box_draw.rectangle((0, 0, 11, 225), outline=(43, 67, 80, 255), width=2)
box.save(out / 'progress_box.png')
bar = Image.new('RGBA', (6, 218), (*blue, 255))
bar.save(out / 'progress_bar.png')
