"""Draw the installable app's icons into static/img/ from the company's palm-tree mark (static/img/palm-mark.png). Safe to run again; it only rewrites those five files.

  python manage.py make_pwa_icons

  pwa-icon-192.png, pwa-icon-512.png     the palm on a white rounded square (a plain "any" icon)
  pwa-icon-maskable-512.png               full-bleed white, the palm inside the central 60% so any phone mask leaves it whole
  apple-touch-icon.png (180)              full-bleed and opaque: iPhones round the corners themselves
  favicon-32.png                          the browser tab: the palm on a transparent background
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from PIL import Image, ImageDraw

SCALE = 4        # drawn four times larger, then reduced, so the edges are smooth
WHITE = (255, 255, 255, 255)


def palm_icon(source, size, share, rounded=False, background=True):
    """One square icon `size` px wide. The palm (cropped to its own edges) is fitted into `share` of the icon and centred. `background`: a white square (rounded when `rounded`),
    else a transparent one."""
    big = size * SCALE
    img = Image.new('RGBA', (big, big), (0, 0, 0, 0))
    if background:
        d = ImageDraw.Draw(img)
        if rounded:
            d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=WHITE)
        else:
            d.rectangle([0, 0, big, big], fill=WHITE)
    mark = source.crop(source.getchannel('A').getbbox())
    room = int(big * share)
    scale = min(room / mark.width, room / mark.height)
    mark = mark.resize((max(1, round(mark.width * scale)), max(1, round(mark.height * scale))), Image.LANCZOS)
    img.alpha_composite(mark, ((big - mark.width) // 2, (big - mark.height) // 2))
    return img.resize((size, size), Image.LANCZOS)


class Command(BaseCommand):
    help = 'Draw the installable app icons into static/img/ from the palm-tree mark.'

    def handle(self, *args, **options):
        out = Path(settings.BASE_DIR) / 'static' / 'img'
        source_path = out / 'palm-mark.png'
        if not source_path.exists():
            raise CommandError('static/img/palm-mark.png (the palm-tree mark, a transparent PNG) is missing.')
        source = Image.open(source_path).convert('RGBA')
        for name, size, share, rounded, background in [('pwa-icon-192.png', 192, 0.72, True, True), ('pwa-icon-512.png', 512, 0.72, True, True), ('pwa-icon-maskable-512.png', 512, 0.58, False, True),
                                                       ('apple-touch-icon.png', 180, 0.72, False, True), ('favicon-32.png', 32, 0.96, False, False)]:
            image = palm_icon(source, size, share, rounded, background)
            if background and not rounded:                     # a full-bleed icon must be opaque
                flat = Image.new('RGB', image.size, WHITE[:3])
                flat.paste(image, mask=image.split()[3])
                image = flat
            image.save(out / name, optimize=True)
            self.stdout.write(f'wrote static/img/{name} ({size}x{size})')
