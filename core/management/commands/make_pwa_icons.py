"""Draw the installable app's icons into static/img/: a white serif "P" on the app's steel blue. Safe to run again; it only rewrites those five files.

  python manage.py make_pwa_icons

  pwa-icon-192.png, pwa-icon-512.png     the icon on a rounded square (a plain "any" icon)
  pwa-icon-maskable-512.png               full-bleed, the P inside the central 60% so any phone mask leaves it whole
  apple-touch-icon.png (180)              full-bleed and opaque: iPhones round the corners themselves
  favicon-32.png                          the browser tab
"""
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from PIL import Image, ImageDraw, ImageFont

from core.pwa import ACCENT

FONTS = [r'C:\Windows\Fonts\georgiab.ttf', r'C:\Windows\Fonts\timesbd.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf', '/System/Library/Fonts/Supplemental/Georgia Bold.ttf']
SCALE = 4        # drawn four times larger, then reduced, so the edges are smooth


def _font(px):
    for path in FONTS:
        if os.path.exists(path):
            return ImageFont.truetype(path, px)
    raise CommandError('No bold serif font was found on this computer. Add a path to FONTS in make_pwa_icons.py.')


def draw_icon(size, rounded, glyph_share):
    """One square icon `size` px wide. `rounded`: a rounded square on a transparent corner (else full-bleed). `glyph_share`: how tall the P is, as a share of the icon."""
    big = size * SCALE
    img = Image.new('RGBA', (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=ACCENT)
    else:
        d.rectangle([0, 0, big, big], fill=ACCENT)
    font = _font(int(big * glyph_share * 1.28))             # a font's size is larger than the height of a capital letter
    box = d.textbbox((0, 0), 'P', font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((big - w) / 2 - box[0], (big - h) / 2 - box[1]), 'P', font=font, fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


class Command(BaseCommand):
    help = 'Draw the installable app icons into static/img/.'

    def handle(self, *args, **options):
        out = Path(settings.BASE_DIR) / 'static' / 'img'
        out.mkdir(parents=True, exist_ok=True)
        for name, size, rounded, share in [('pwa-icon-192.png', 192, True, 0.56), ('pwa-icon-512.png', 512, True, 0.56), ('pwa-icon-maskable-512.png', 512, False, 0.42),
                                           ('apple-touch-icon.png', 180, False, 0.56), ('favicon-32.png', 32, True, 0.62)]:
            image = draw_icon(size, rounded, share)
            if not rounded:                                    # a full-bleed icon must be opaque
                flat = Image.new('RGB', image.size, ACCENT)
                flat.paste(image, mask=image.split()[3])
                image = flat
            image.save(out / name, optimize=True)
            self.stdout.write(f'wrote static/img/{name} ({size}x{size})')
