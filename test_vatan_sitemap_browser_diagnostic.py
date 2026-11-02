"""Read-only diagnostic never publishes solver bodies or enlarges source limits."""

import json
import unittest
from unittest.mock import patch

from audit_vatan_sitemap_browser import diagnose_solver_response
from vatan_sitemap_source import SITEMAP_URL, SOLVER_URL


class Response:
    def __init__(self, body, status=200):
        self.status_code = status
        self.body = body if isinstance(body, bytes) else body.encode()
        self.headers = {'Content-Length': str(len(self.body))}
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


def solver_response(body):
    return Response(json.dumps({'status': 'ok', 'solution': {
        'status': 200, 'url': SITEMAP_URL, 'response': body,
    }}).encode())


class VatanSitemapBrowserDiagnosticTests(unittest.TestCase):
    def test_reports_sizes_and_shape_without_retaining_xml_or_cookies(self):
        xml = ('<?xml version="1.0"?><urlset '
               'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               '<url><loc>https://www.vatanbilgisayar.com/example.html</loc></url>'
               '</urlset>')
        response = solver_response(xml)
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs['json']))
            return response

        result = diagnose_solver_response(post)
        self.assertEqual(calls, [(SOLVER_URL, {
            'cmd': 'request.get', 'url': SITEMAP_URL, 'maxTimeout': 45000,
        })])
        self.assertTrue(response.closed)
        self.assertEqual(result['status'], 'measured')
        self.assertEqual(result['solver_bytes'], len(response.body))
        self.assertEqual(result['response_bytes'], len(xml.encode()))
        self.assertEqual((result['response_kind'], result['structural_urlset'], result['url_count']),
                         ('raw_xml', True, 1))
        self.assertNotIn('response', result)
        self.assertNotIn('cookies', result)

    def test_html_is_classified_but_never_treated_as_sitemap(self):
        result = diagnose_solver_response(lambda *_a, **_k: solver_response('<html>denied</html>'))
        self.assertEqual(result['status'], 'measured')
        self.assertEqual(result['response_kind'], 'html')
        self.assertNotIn('structural_urlset', result)

    def test_diagnostic_has_its_own_hard_byte_cap(self):
        response = solver_response('x' * 512)
        with patch('audit_vatan_sitemap_browser.MAX_DIAGNOSTIC_SOLVER_BYTES', 200):
            result = diagnose_solver_response(lambda *_a, **_k: response)
        self.assertEqual(result['status'], 'solver_response_too_large')
        self.assertEqual(result['solver_bytes_at_abort'], len(response.body))
        self.assertIsNone(result['response_bytes'])
        self.assertTrue(response.closed)


if __name__ == '__main__':
    unittest.main()
