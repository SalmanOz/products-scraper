import unittest
from xml.sax.saxutils import escape

from vatan_sitemap_source import (
    ROBOTS_URL, SITEMAP_URL, VatanSitemapSource,
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
        self.assertEqual(fetch.calls, [ROBOTS_URL, SITEMAP_URL])
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

    def test_missing_or_disallowing_robots_fails_closed_once(self):
        for robots in (b'<html>Unavailable</html>', f'User-agent: *\nDisallow: /\nSitemap: {SITEMAP_URL}\n'.encode()):
            with self.subTest(robots=robots):
                fetch = Fetch({ROBOTS_URL: robots})
                source = VatanSitemapSource(fetch=fetch)
                self.assertEqual(source.discover('Xiaomi 17T', {
                    'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g',
                }), [])
                self.assertEqual(source.discover('Xiaomi 17T', {
                    'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g',
                }), [])
                self.assertEqual(fetch.calls, [ROBOTS_URL])

    def test_reports_exact_failed_stage_and_http_status(self):
        cases = (
            ({ROBOTS_URL: Response(ROBOTS_URL, b'blocked', status=403)},
             'robots_http_403', 'robots', 403),
            ({ROBOTS_URL: ROBOTS,
              SITEMAP_URL: Response(SITEMAP_URL, b'blocked', status=429)},
             'sitemap_http_429', 'sitemap', 429),
            ({ROBOTS_URL: Response('https://other.example/robots.txt', ROBOTS)},
             'robots_unexpected_final_url', 'robots', 200),
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

    def test_obeys_robots_crawl_delay_between_requests(self):
        robots = ROBOTS.replace(b'Disallow: /arama/*', b'Crawl-delay: 2\nDisallow: /arama/*')
        fetch = Fetch({ROBOTS_URL: robots, SITEMAP_URL: sitemap()})
        delays = []
        source = VatanSitemapSource(fetch=fetch, sleep=delays.append)
        source.discover('Xiaomi 17T', {
            'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g',
        })
        self.assertEqual(delays, [2.0])


if __name__ == '__main__':
    unittest.main()
