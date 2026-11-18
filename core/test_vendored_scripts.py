from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class VendoredScriptTests(SimpleTestCase):
    """The menu scripts (htmx, Lucide icons) come from our own server, pinned to the exact files that used to come from unpkg, so the page does not wait on a third-party host to draw."""

    base = Path(settings.BASE_DIR)

    def test_the_two_files_are_served_from_our_own_static_folder(self):
        htmx = self.base / 'static/vendor/htmx-1.9.12/htmx.min.js'
        lucide = self.base / 'static/vendor/lucide-1.52.0/lucide.min.js'
        self.assertGreater(htmx.stat().st_size, 40000)
        self.assertGreater(lucide.stat().st_size, 400000)
        self.assertIn('htmx', htmx.read_text(encoding='utf8')[:400].lower())
        self.assertIn('lucide', lucide.read_text(encoding='utf8')[:400].lower())

    def test_the_icon_file_does_not_point_at_a_source_map_that_is_not_there(self):
        self.assertNotIn('sourceMappingURL', (self.base / 'static/vendor/lucide-1.52.0/lucide.min.js').read_text(encoding='utf8'))

    def test_the_base_template_loads_them_from_static_and_not_from_unpkg(self):
        html = (self.base / 'templates/base.html').read_text(encoding='utf8')
        self.assertIn("{% static 'vendor/htmx-1.9.12/htmx.min.js' %}", html)
        self.assertIn("{% static 'vendor/lucide-1.52.0/lucide.min.js' %}", html)
        self.assertNotIn('unpkg.com', html)
        self.assertLess(html.index('vendor/htmx-1.9.12'), html.index('vendor/lucide-1.52.0'))          # same order as before
