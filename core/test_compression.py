"""Pages the app builds are compressed for a browser that accepts it; downloads and tiny replies are left alone."""
import gzip

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from .middleware import CompressTextMiddleware


def respond(body, content_type='text/html; charset=utf-8', **headers):
    def view(request):
        r = HttpResponse(body, content_type=content_type)
        for k, v in headers.items():
            r[k.replace('_', '-')] = v
        return r
    return view


class CompressText(TestCase):
    def call(self, view, accept='gzip, deflate'):
        request = RequestFactory().get('/x/', HTTP_ACCEPT_ENCODING=accept)
        return CompressTextMiddleware(view)(request)

    def test_a_big_page_is_compressed_when_the_browser_accepts_it(self):
        body = '<html>' + '<tr><td>row</td></tr>' * 500 + '</html>'
        r = self.call(respond(body))
        self.assertEqual(r['Content-Encoding'], 'gzip')
        self.assertLess(len(r.content), len(body) // 5)
        self.assertEqual(gzip.decompress(r.content).decode(), body)
        self.assertIn('Accept-Encoding', r['Vary'])

    def test_json_is_compressed_too(self):
        r = self.call(respond('{"a": "' + 'x' * 2000 + '"}', 'application/json'))
        self.assertEqual(r['Content-Encoding'], 'gzip')

    def test_a_browser_that_does_not_ask_gets_the_page_as_it_is(self):
        r = self.call(respond('<html>' + 'x' * 2000 + '</html>'), accept='identity')
        self.assertFalse(r.has_header('Content-Encoding'))

    def test_a_pdf_a_photo_and_a_download_are_left_alone(self):
        for ctype, extra in (('application/pdf', {}), ('image/png', {}), ('text/csv', {'Content_Disposition': 'attachment; filename="a.csv"'})):
            r = self.call(respond('x' * 5000, ctype, **extra))
            self.assertFalse(r.has_header('Content-Encoding'), ctype)

    def test_a_tiny_reply_is_not_worth_compressing(self):
        r = self.call(respond('ok'))
        self.assertFalse(r.has_header('Content-Encoding'))

    def test_the_real_stack_serves_a_compressed_screen(self):
        user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(user)
        r = self.client.get('/accounting/', HTTP_ACCEPT_ENCODING='gzip')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get('Content-Encoding'), 'gzip')
