import json
import unittest
from xml.sax.saxutils import escape

from vatan_sitemap_source import (
    ROBOTS_URL, SITEMAP_URL, SOLVER_URL, VatanSitemapSource,
)


ROBOTS = f'''User-agent: *
Disallow: /arama/*
Sitemap: {SITEMAP_URL}
'''.encode()


def sitemap(*urls):
    rows = ''.join(f'<url><loc>{escape(url)}</loc></url>' for url in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + rows + '</urlset>').encode()


class Response:
    def __init__(self, url, data, status=200, content_length=None):
        self.url, self.data, self.status_code = url, data, status
        self.headers = {'Content-Length': str(content_length if content_length is not None else len(data))}

    def iter_content(self, chunk_size):
        for index in range(0, len(self.data), chunk_size):
            yield self.data[index:index + chunk_size]

    def close(self):
        pass


class Fetch:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        if url not in self.pages:
            raise AssertionError(f'Unexpected request: {url}')
        value = self.pages[url]
        return value if isinstance(value, Response) else Response(url, value)


class VatanSitemapSourceTests(unittest.TestCase):
    def test_exact_unitless_phone_pair_reaches_catalog_reader_without_a_profile(self):
        from vatan_reviewed_source import VatanReviewedSource
        url = ('https://www.vatanbilgisayar.com/'
               'oppo-find-x9-pro-16-512-akilli-telefon-titanyum-antrasit.html')
        specs = {'ram_gb': 16, 'storage_gb': 512, 'network_generation': '5g'}
        source = VatanSitemapSource(fetch=Fetch({ROBOTS_URL: ROBOTS,
                                                SITEMAP_URL: sitemap(url)}))
        targets = source.discover('OPPO Find X9 Pro', specs)
        self.assertEqual(targets, [{'source_url': url}])
        reader = VatanReviewedSource()
        reader.register_catalog_targets('OPPO Find X9 Pro', targets, specs)
        self.assertTrue(reader.is_reviewed(url))
        for wrong in ({**specs, 'ram_gb': 12}, {**specs, 'storage_gb': 256}):
            self.assertEqual(source.discover('OPPO Find X9 Pro', wrong), [])
        for path in ('/oppo-find-x9-pro-16-512.html',
                     '/oppo-find-x9-16-512-akilli-telefon.html',
                     '/oppo-find-x9-pro-512-akilli-telefon.html'):
            self.assertFalse(source._variant_matches(path, 'OPPO Find X9 Pro', specs))

    def test_storage_before_ram_is_a_hint_without_accepting_a_capacity_conflict(self):
        specs = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
        for path in ('/samsung-galaxy-a57-5g-128gb-8gb',
                     '/samsung-galaxy-a57-5g-8gb-ram128gb-hafiza-android-telefon'):
            self.assertTrue(VatanSitemapSource._variant_matches(path, 'Samsung Galaxy A57 5G', specs))
            self.assertFalse(VatanSitemapSource._variant_matches(path, 'Samsung Galaxy A57 5G',
                             {**specs, 'ram_gb': 6}))
            self.assertFalse(VatanSitemapSource._variant_matches(path, 'Samsung Galaxy A57 5G',
                             {**specs, 'storage_gb': 256}))
    def test_chrome_xml_viewer_yields_only_bounded_original_sitemap_hints(self):
        exact = 'https://www.vatanbilgisayar.com/xiaomi-17t-12-256gb-akilli-telefon-siyah.html'
        xml = sitemap(exact).decode().split('?>', 1)[1]
        source_html = ('<!DOCTYPE html><html><body><div id="viewer">'
                       + ' ' * (3 * 1024 * 1024) + '</div>'
                       + '<div id="webkit-xml-viewer-source-xml">' + xml + '</div>'
                       + '</body></html>')
        for document, expected in (
            (source_html, 'ready'),
            (source_html.replace('schemas/sitemap/0.9', 'schemas/other/0.9'),
             'sitemap_browser_invalid_xml_wrapper'),
            (source_html.replace('</body>', '<div id="webkit-xml-viewer-source-xml"></div></body>'),
             'sitemap_browser_invalid_xml_wrapper'),
            (source_html.replace('<urlset ', '<!ENTITY x "bad"><urlset '), 'unsafe_xml'),
        ):
            with self.subTest(expected=expected):
                fetch = Fetch({ROBOTS_URL: ROBOTS,
                               SITEMAP_URL: Response(SITEMAP_URL, b'denied', status=403)})
                def post(url, **_kwargs):
                    return Response(url, json.dumps({'status': 'ok', 'solution': {
                        'status': 200, 'url': SITEMAP_URL, 'response': document}}).encode())
                source = VatanSitemapSource(fetch=fetch, post=post, sleep=lambda _: None)
                hints = source.discover('Xiaomi 17T', {'ram_gb': 12, 'storage_gb': 256})
                self.assertEqual(source.report()['status'], expected)
                self.assertEqual(hints, [{'source_url': exact}] if expected == 'ready' else [])
                self.assertEqual(source.report()['browser_requests'], 1)
                if expected == 'ready':
                    self.assertTrue(source.report()['browser_xml_viewer_unwrapped'])

    def test_sitemap_403_uses_one_bounded_browser_get(self):
        exact = 'https://www.vatanbilgisayar.com/xiaomi-17t-12-256gb-akilli-telefon-siyah.html'
        direct = Fetch({ROBOTS_URL: ROBOTS,
                        SITEMAP_URL: Response(SITEMAP_URL, b'denied', status=403)})
        browser_calls = []

        def post(url, **kwargs):
            browser_calls.append((url, kwargs['json']))
            return Response(url, json.dumps({'status': 'ok', 'solution': {
                'status': 200, 'url': SITEMAP_URL, 'response': sitemap(exact).decode(),
            }}).encode())

        delays = []
        source = VatanSitemapSource(fetch=direct, post=post, sleep=delays.append)
        specs = {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        self.assertEqual(source.discover('Xiaomi 17T', specs), [{'source_url': exact}])
        self.assertEqual(source.discover('Xiaomi 17T', specs), [{'source_url': exact}])
        self.assertEqual(direct.calls, [SITEMAP_URL])
        self.assertEqual(browser_calls, [(SOLVER_URL, {
            'cmd': 'request.get', 'url': SITEMAP_URL, 'maxTimeout': 45000,
        })])
        self.assertEqual(delays, [0.5])
        self.assertEqual(source.report()['sitemap_transport'], 'browser')
        self.assertEqual(source.report()['sitemap_direct_http_status'], 403)
        self.assertEqual(source.report()['browser_requests'], 1)

    def test_browser_sitemap_failures_cannot_create_candidates(self):
        exact = 'https://www.vatanbilgisayar.com/xiaomi-17t-12-256gb-akilli-telefon-siyah.html'
        valid_xml = sitemap(exact).decode()
        cases = (
            ({'status': 200, 'url': SITEMAP_URL + '?changed=1', 'response': valid_xml},
             'sitemap_browser_changed_url'),
            ({'status': 403, 'url': SITEMAP_URL, 'response': valid_xml},
             'sitemap_browser_http_403'),
            ({'status': 200, 'url': SITEMAP_URL, 'response': '<html>challenge</html>'},
             'not_urlset'),
            ({'status': 200, 'url': SITEMAP_URL, 'response':
              '<!DOCTYPE urlset [<!ENTITY x "bad">]>' + valid_xml},
             'unsafe_xml'),
        )
        for solution, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                fetch = Fetch({ROBOTS_URL: ROBOTS,
                               SITEMAP_URL: Response(SITEMAP_URL, b'denied', status=403)})
                post = lambda url, **_kwargs: Response(url, json.dumps({
                    'status': 'ok', 'solution': solution,
                }).encode())
                source = VatanSitemapSource(fetch=fetch, post=post, sleep=lambda _: None)
                self.assertEqual(source.discover('Xiaomi 17T', {
                    'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g',
                }), [])
                self.assertEqual(source.report()['status'], expected_status)
                self.assertEqual(source.report()['eligible_urls'], 0)
                self.assertEqual(source.report()['browser_requests'], 1)

    def test_sitemap_rate_limit_never_calls_browser(self):
        for pages in (
            {ROBOTS_URL: ROBOTS,
             SITEMAP_URL: Response(SITEMAP_URL, b'rate limited', status=429)},
        ):
            with self.subTest(pages=pages):
                post_calls = []
                source = VatanSitemapSource(fetch=Fetch(pages),
                    post=lambda *_args, **_kwargs: post_calls.append(True),
                    sleep=lambda _: None)
                self.assertEqual(source.discover('Xiaomi 17T', {
                    'ram_gb': 12, 'storage_gb': 256,
                }), [])
                self.assertEqual(post_calls, [])
                self.assertEqual(source.report()['browser_requests'], 0)

    def test_discovers_only_exact_model_memory_radio_and_caches_sitemap(self):
        exact = 'https://www.vatanbilgisayar.com/xiaomi-17t-12-256gb-akilli-telefon-siyah.html'
        wrong_ram = 'https://www.vatanbilgisayar.com/xiaomi-17t-8-256gb-akilli-telefon-mor.html'
        wrong_storage = 'https://www.vatanbilgisayar.com/xiaomi-17t-12-512gb-akilli-telefon-beyaz.html'
        wrong_model = 'https://www.vatanbilgisayar.com/xiaomi-17t-pro-12-256gb-akilli-telefon-siyah.html'
        wrong_radio = 'https://www.vatanbilgisayar.com/redmi-note-15-4g-12-256gb-akilli-telefon-siyah.html'
        disallowed_search = 'https://www.vatanbilgisayar.com/arama/xiaomi-17t-akilli-telefon.html'
        fetch = Fetch({ROBOTS_URL: ROBOTS, SITEMAP_URL: sitemap(
            exact, wrong_ram, wrong_storage, wrong_model, wrong_radio,
            disallowed_search, 'https://attacker.example/xiaomi-17t-12-256gb-akilli-telefon.html')})
        source = VatanSitemapSource(fetch=fetch)
        expected = {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        self.assertEqual(source.discover('Xiaomi 17T', expected), [{'source_url': exact}])
        # Public catalog summaries omit Connectivity; this remains only a URL
        # hint until the exact PDP reader verifies the actual offer.
        self.assertEqual(source.discover('Xiaomi 17T', {
            'ram_gb': 12, 'storage_gb': 256,
        }), [{'source_url': exact}])
        self.assertEqual(source.discover('Xiaomi 17T', expected), [{'source_url': exact}])
        self.assertEqual(fetch.calls, [SITEMAP_URL])
        self.assertEqual(source.report()['status'], 'ready')

    def test_explicit_radio_conflict_is_rejected(self):
        self.assertFalse(VatanSitemapSource._variant_matches(
            '/tecno-spark-slim-5g-8-256gb-akilli-telefon.html',
            'Tecno Spark Slim', {'ram_gb': 8, 'storage_gb': 256,
                                 'network_generation': '4g'},
        ))

    def test_iphone_needs_explicit_storage_not_generic_family_url(self):
        exact = 'https://www.vatanbilgisayar.com/iphone-17-256-gb-akilli-telefon-beyaz.html'
        generic = 'https://www.vatanbilgisayar.com/iphone-17-akilli-telefon.html'
        fetch = Fetch({ROBOTS_URL: ROBOTS, SITEMAP_URL: sitemap(generic, exact)})
        source = VatanSitemapSource(fetch=fetch)
        self.assertEqual(source.discover('Apple iPhone 17', {
            'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g',
        }), [{'source_url': exact}])

    def test_reports_exact_failed_stage_and_http_status(self):
        cases = (
            ({ROBOTS_URL: ROBOTS,
              SITEMAP_URL: Response(SITEMAP_URL, b'blocked', status=429)},
             'sitemap_http_429', 'sitemap', 429),
            ({SITEMAP_URL: Response('https://other.example/sitemap.xml', sitemap())},
             'sitemap_unexpected_final_url', 'sitemap', 200),
        )
        for pages, status, stage, http_status in cases:
            with self.subTest(status=status):
                source = VatanSitemapSource(fetch=Fetch(pages))
                self.assertEqual(source.discover('Xiaomi 17T', {
                    'ram_gb': 12, 'storage_gb': 256,
                }), [])
                self.assertEqual(source.report()['status'], status)
                self.assertEqual(source.report()['failed_stage'], stage)
                self.assertEqual(source.report()['http_status'], http_status)

    def test_oversized_or_unsafe_sitemap_fails_closed(self):
        oversized = Response(SITEMAP_URL, b'<urlset/>', content_length=4 * 1024 * 1024)
        unsafe = b'<!DOCTYPE urlset [<!ENTITY x "bad">]><urlset>&x;</urlset>'
        for value in (oversized, unsafe):
            with self.subTest(value=value):
                fetch = Fetch({ROBOTS_URL: ROBOTS, SITEMAP_URL: value})
                source = VatanSitemapSource(fetch=fetch)
                self.assertEqual(source.discover('Xiaomi 17T', {
                    'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g',
                }), [])
                self.assertNotEqual(source.report()['status'], 'ready')

if __name__ == '__main__':
    unittest.main()
