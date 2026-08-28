#!/usr/bin/env python3
from pathlib import Path
import sys
from PIL import Image, ImageDraw, ImageFont

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('.')
out.mkdir(parents=True, exist_ok=True)
W, H = 800, 480
blue = (28, 190, 255)
dim = (86, 126, 146)
img = Image.new('RGB', (W, H), (0, 0, 0))
draw = ImageDraw.Draw(img)
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
    x = (W - (box[2] - box[0])) // 2
    draw.text((x, y), text, font=font, fill=fill)

centered('RF EYE', 170, title, blue)
centered('STARTING SYSTEM', 266, subtitle, dim)
img.save(out / 'splash.png')

box = Image.new('RGBA', (500, 14), (22, 31, 38, 255))
box_draw = ImageDraw.Draw(box)
box_draw.rectangle((0, 0, 499, 13), outline=(43, 67, 80, 255), width=2)
box.save(out / 'progress_box.png')

bar = Image.new('RGBA', (492, 6), (*blue, 255))
bar.save(out / 'progress_bar.png')
