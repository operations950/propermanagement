"""Photos over the storage limit are shrunk to fit; everything else is stored as it was."""
import io
import os
import tempfile

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import SimpleTestCase, override_settings

from core import imaging
from core.storage import DocumentStorage

from PIL import Image


def noisy_image(side=1400, fmt='PNG'):
    """A picture that does not compress (random pixels), so it is big for its size."""
    img = Image.frombytes('RGB', (side, side), os.urandom(side * side * 3))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


class ShrinkTests(SimpleTestCase):
    def test_a_photo_under_the_limit_is_left_alone(self):
        data = noisy_image(200)
        content = ContentFile(data, name='small.png')
        name, out = imaging.shrink_photo('docs/small.png', content, max_bytes=10_000_000)
        self.assertEqual(name, 'docs/small.png')
        self.assertIs(out, content)

    def test_a_photo_over_the_limit_comes_back_as_a_jpeg_that_fits(self):
        data = noisy_image(1400)                                  # about 5.9 MB as a PNG
        self.assertGreater(len(data), 3_000_000)
        name, out = imaging.shrink_photo('onsite_visit_media/2026/11/IMG_0001.png', ContentFile(data, name='IMG_0001.png'), max_bytes=800_000)
        self.assertEqual(name, 'onsite_visit_media/2026/11/IMG_0001.jpg')
        out.seek(0)
        raw = out.read()
        self.assertLessEqual(len(raw), 800_000)
        img = Image.open(io.BytesIO(raw))
        self.assertEqual(img.format, 'JPEG')
        self.assertGreater(max(img.size), 400)                    # still a usable picture, not a thumbnail

    def test_the_picture_is_turned_the_way_up_it_was_taken(self):
        img = Image.frombytes('RGB', (1200, 600), os.urandom(1200 * 600 * 3))
        exif = Image.Exif()
        exif[0x0112] = 6                                          # a phone held upright: rotate 90 degrees to view
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=100, exif=exif)
        name, out = imaging.shrink_photo('a.jpg', ContentFile(buf.getvalue(), name='a.jpg'), max_bytes=200_000)
        out.seek(0)
        shrunk = Image.open(io.BytesIO(out.read()))
        self.assertGreater(shrunk.size[1], shrunk.size[0])        # now taller than wide, with no orientation tag left to apply

    def test_a_transparent_png_becomes_a_jpeg_on_white(self):
        img = Image.new('RGBA', (900, 900), (0, 0, 0, 0))
        noise = Image.frombytes('RGB', (900, 900), os.urandom(900 * 900 * 3)).convert('RGBA')
        img.paste(noise.crop((0, 0, 450, 900)), (0, 0))
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        name, out = imaging.shrink_photo('t.png', ContentFile(buf.getvalue(), name='t.png'), max_bytes=300_000)
        out.seek(0)
        shrunk = Image.open(io.BytesIO(out.read()))
        self.assertEqual(shrunk.mode, 'RGB')
        self.assertGreater(sum(shrunk.getpixel((880, 10))) / 3, 240)         # the transparent corner is white, not black

    def test_other_files_are_never_touched_however_big(self):
        for name in ('statement.pdf', 'tour.mp4', 'photo.heic', 'booking.csv', 'noextension'):
            content = ContentFile(b'x' * 2_000_000, name=name)
            out_name, out = imaging.shrink_photo(name, content, max_bytes=1_000)
            self.assertEqual(out_name, name)
            self.assertIs(out, content)

    def test_a_file_named_like_a_photo_that_is_not_one_is_stored_as_it_is(self):
        content = ContentFile(b'this is not a picture' * 100_000, name='broken.jpg')
        name, out = imaging.shrink_photo('broken.jpg', content, max_bytes=1_000)
        self.assertEqual(name, 'broken.jpg')
        out.seek(0)
        self.assertEqual(out.read(), b'this is not a picture' * 100_000)

    def test_the_default_limit_sits_under_clouds_ten_megabytes(self):
        self.assertLess(imaging.MAX_BYTES, imaging.LIMIT_BYTES)
        self.assertEqual(imaging.LIMIT_BYTES, 10_485_760)


class DocumentStorageSaveTests(SimpleTestCase):
    def test_an_upload_field_stores_the_shrunk_photo_under_a_jpg_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            storages = {'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': tmp}},
                        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': tmp}},
                        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
            with override_settings(STORAGES=storages):
                from unittest import mock
                with mock.patch.object(imaging, 'MAX_BYTES', 800_000):
                    saved = DocumentStorage().save('visit/IMG_9.png', ContentFile(noisy_image(1400), name='IMG_9.png'))
                self.assertTrue(saved.endswith('.jpg'), saved)
                self.assertLessEqual(os.path.getsize(os.path.join(tmp, saved)), 800_000)
                pdf = DocumentStorage().save('visit/a.pdf', ContentFile(b'%PDF-1.4 ' + b'x' * 2_000_000, name='a.pdf'))
                self.assertEqual(os.path.getsize(os.path.join(tmp, pdf)), 2_000_009)
