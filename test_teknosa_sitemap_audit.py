import json
import unittest

from teknosa_sitemap_audit import (
    INDEX_URL, MAX_PRODUCT_BYTES, ROBOTS_URL, SOLVER_URL,
    TeknosaSitemapAudit,
)


CHILD = ('https://www.teknosa.com/medias/sys_master/sitemaps/sitemaps/'
         '1302120709/Product-tr-TRY-0.xml')
ROBOTS = f'User-agent: *\nDisallow: /sepet\nSitemap: {INDEX_URL}\n'


def xml(tag, locs):
    row = 'sitemap' if tag == 'sitemapindex' else 'url'
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<' + tag + ' xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + ''.join(f'<{row}><loc>{loc}</loc></{row}>' for loc in locs)
            + '</' + tag + '>')


INDEX = xml('sitemapindex', [CHILD])
PRODUCT = xml('urlset', [
    'https://www.teknosa.com/apple-iphone-17e-256gb-beyaz-akilli-telefon-p-100000061663',
    'https://www.teknosa.com/apple-iphone-17e-512gb-beyaz-akilli-telefon-p-100000061664',
    'https://www.teknosa.com/apple-iphone-16e-128gb-siyah-akilli-telefon-p-125076211',
    'https://www.teknosa.com/samsung-galaxy-s25-fe-5g-8256gb-gece-siyahi-akilli-telefon-p-100000058513',
    'https://www.teknosa.com/samsung-galaxy-a26-6128gb-akilli-telefon-beyaz-p-125070166',
    'https://www.teknosa.com/apple-iphone-18-pro-256gb-siyah-akilli-telefon-p-100000065149',
    'https://www.teknosa.com/apple-iphone-18-pro-max-256gb-siyah-akilli-telefon-p-100000065168',
    'https://www.teknosa.com/apple-iphone-16e-128gb-siyah-outlet-p-202601210055',
])


class Response:
    def __init__(self, url, status, body):
        self.url = url
        self.status_code = status
        self._body = body.encode() if isinstance(body, str) else body
        self.headers = {'Content-Length': str(len(self._body))}

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        pass


class TeknosaSitemapAuditTests(unittest.TestCase):
    def test_one_advertised_shard_yields_only_bounded_exact_variant_hints(self):
        payload = {ROBOTS_URL: ROBOTS, INDEX_URL: INDEX, CHILD: PRODUCT}
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            return Response(url, 200, payload[url])
        report = TeknosaSitemapAudit(get=get, post=None, sleep=lambda _: None).run()
        self.assertEqual((report['status'], report['stage'], report['product_url_count']),
                         ('completed', None, 8))
        self.assertEqual(calls, [ROBOTS_URL, INDEX_URL, CHILD])
        self.assertEqual(report['browser_requests'], 0)
        self.assertIs(report['writes'], False)
        self.assertEqual({key: len(urls) for key, urls in report['candidate_urls'].items()},
                         {'apple-iphone-17e-8-256': 1,
                          'apple-iphone-16e-8-128': 1,
                          'samsung-galaxy-s25-fe-8-256': 1,
                          'samsung-galaxy-a26-6-128': 1,
                          'apple-iphone-18-pro-12-256': 1,
                          'apple-iphone-18-pro-max-12-256': 1})

    def test_403_browser_fallback_is_exact_url_and_three_request_bounded(self):
        viewer = ('<html><head><style id="xml-viewer-style"></style></head><body>'
                  '<div id="webkit-xml-viewer-source-xml">' + INDEX + '</div>'
                  '<div class="header"></div><div class="pretty-print"></div>'
                  '</body></html>')
        payload = {ROBOTS_URL: '<html><body><pre>' + ROBOTS + '</pre></body></html>',
                   INDEX_URL: viewer, CHILD: PRODUCT}
        calls = []
        def get(url, **kwargs):
            calls.append(('direct', url))
            return Response(url, 403, 'denied')
        def post(url, *, json, **kwargs):
            target = json['url']
            calls.append(('browser', target))
            body = __import__('json').dumps({'status': 'ok', 'solution': {
                'status': 200, 'url': target, 'response': payload[target],
            }})
            return Response(url, 200, body)
        report = TeknosaSitemapAudit(get=get, post=post, sleep=lambda _: None).run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['browser_requests'], 3)
        self.assertEqual(report['transports'], {
            ROBOTS_URL: 'browser', INDEX_URL: 'browser', CHILD: 'browser'})
        self.assertEqual(calls, [('direct', ROBOTS_URL), ('browser', ROBOTS_URL),
                                 ('direct', INDEX_URL), ('browser', INDEX_URL),
                                 ('direct', CHILD), ('browser', CHILD)])

    def test_double_rendered_xml_can_exceed_15_mib_without_relaxing_xml_limit(self):
        # FlareSolverr serializes the browser's XML viewer as JSON. The pretty
        # tree can duplicate the original XML; only source XML may be parsed.
        viewer = ('<html><body><div id="webkit-xml-viewer-source-xml">'
                  + PRODUCT + '</div><div class="pretty-print">'
                  + ('x' * (16 * 1024 * 1024)) + '</div></body></html>')
        payload = {ROBOTS_URL: ROBOTS, INDEX_URL: INDEX}
        def get(url, **kwargs):
            return Response(url, 403 if url == CHILD else 200,
                            'denied' if url == CHILD else payload[url])
        def post(url, *, json, **kwargs):
            body = __import__('json').dumps({'status': 'ok', 'solution': {
                'status': 200, 'url': CHILD, 'response': viewer,
            }})
            return Response(url, 200, body)
        report = TeknosaSitemapAudit(get=get, post=post, sleep=lambda _: None).run()
        self.assertEqual(report['status'], 'completed')
        diagnostic = report['solver_diagnostics'][CHILD]
        self.assertGreater(diagnostic['received_bytes'], 15 * 1024 * 1024)
        self.assertEqual(diagnostic['response_kind'], 'chromium_xml_viewer')
        self.assertLess(report['xml_diagnostics'][CHILD]['xml_bytes'], MAX_PRODUCT_BYTES)

    def test_robots_or_xml_drift_stops_before_product_sitemap(self):
        cases = [
            ({ROBOTS_URL: ROBOTS.replace('Disallow: /sepet', 'Disallow: /')},
             'index_disallowed', [ROBOTS_URL]),
            ({ROBOTS_URL: ROBOTS, INDEX_URL: '<!DOCTYPE x><sitemapindex />'},
             'unsafe_or_oversized_xml', [ROBOTS_URL, INDEX_URL]),
            ({ROBOTS_URL: ROBOTS,
              INDEX_URL: xml('sitemapindex', [
                  'https://example.com/medias/sys_master/sitemaps/sitemaps/1/Product-tr-TRY-0.xml'])},
             'first_product_sitemap_missing_or_ambiguous', [ROBOTS_URL, INDEX_URL]),
        ]
        for overrides, error, expected_calls in cases:
            with self.subTest(error=error):
                payload = {ROBOTS_URL: ROBOTS, INDEX_URL: INDEX, CHILD: PRODUCT,
                           **overrides}
                calls = []
                def get(url, **kwargs):
                    calls.append(url)
                    return Response(url, 200, payload[url])
                report = TeknosaSitemapAudit(get=get, sleep=lambda _: None).run()
                self.assertEqual(report['error'], error)
                self.assertEqual(calls, expected_calls)
                self.assertEqual(report['candidate_urls'], {})

    def test_response_size_is_hard_capped(self):
        payload = {ROBOTS_URL: ROBOTS, INDEX_URL: INDEX,
                   CHILD: b'x' * (MAX_PRODUCT_BYTES + 1)}
        def get(url, **kwargs):
            return Response(url, 200, payload[url])
        report = TeknosaSitemapAudit(get=get, sleep=lambda _: None).run()
        self.assertEqual(report['error'], 'direct_oversized')
        self.assertEqual(report['product_url_count'], 0)


if __name__ == '__main__':
    unittest.main()
