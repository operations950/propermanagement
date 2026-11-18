"""Photos that are too big to store are shrunk, not refused.

Cloudinary turns away any file over 10 MB (10,485,760 bytes): a cleaner's phone, a high-resolution camera or a scan can easily make a photo that size, and the upload used to fail with
"upload failed on our end". Every upload field in the app goes through core.storage.DocumentStorage, which calls `shrink_photo` first: a photo over MAX_BYTES is turned into a JPEG that
fits (same picture, the right way up, scaled down only as far as it has to be). Anything else (a small photo, a PDF, a video, a photo format Pillow cannot open) is handed back untouched."""
import io
import logging
import os

from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

LIMIT_BYTES = 10 * 1024 * 1024              # what Cloudinary refuses
MAX_BYTES = 9_500_000                       # what a shrunk photo is made to fit under (a little under the limit)
SHRINKABLE = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff')
QUALITIES = (88, 80, 72)                    # tried in this order at each size
SCALE_STEP = 0.8                            # each pass makes the longest side this much shorter
SMALLEST_SIDE = 800                         # never made smaller than this (the longest side)


def _size(content):
    size = getattr(content, 'size', None)
    if size is not None:
        return size
    content.seek(0, os.SEEK_END)
    size = content.tell()
    content.seek(0)
    return size


def shrink_photo(name, content, max_bytes=None):
    """(name, content) ready to store. A photo larger than `max_bytes` comes back as a JPEG (the name's extension becomes .jpg) that fits; everything else comes back as it was given."""
    max_bytes = max_bytes or MAX_BYTES
    ext = os.path.splitext(str(name))[1].lower()
    if ext not in SHRINKABLE:
        return name, content
    try:
        if _size(content) <= max_bytes:
            return name, content
    except Exception:
        return name, content
    try:
        from PIL import Image, ImageOps
        content.seek(0)
        img = Image.open(content)
        img.load()
        img = ImageOps.exif_transpose(img)                       # the way up the phone meant it, since the shrunk copy carries no orientation tag
        if img.mode in ('RGBA', 'LA', 'P'):
            rgba = img.convert('RGBA')
            flat = Image.new('RGB', rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
            img = flat
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        scale, best = 1.0, None
        while True:
            w, h = img.size
            frame = img if scale == 1.0 else img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            for quality in QUALITIES:
                buf = io.BytesIO()
                frame.save(buf, 'JPEG', quality=quality, optimize=True)
                if best is None or buf.tell() < best.tell():
                    best = buf
                if buf.tell() <= max_bytes:
                    root = os.path.splitext(str(name))[0]
                    new_name = root + '.jpg'
                    return new_name, ContentFile(buf.getvalue(), name=os.path.basename(new_name))
            if max(frame.size) * SCALE_STEP < SMALLEST_SIDE:
                break
            scale *= SCALE_STEP
        # could not be made to fit (a huge photo of pure noise): store the smallest we got; the storage will say so if it is still refused
        root = os.path.splitext(str(name))[0]
        return root + '.jpg', ContentFile(best.getvalue(), name=os.path.basename(root) + '.jpg')
    except Exception:
        logger.exception('could not shrink %s; storing it as it is', name)
        try:
            content.seek(0)
        except Exception:
            pass
        return name, content
