"""Offline robots, category and exact-variant tests for Pazarama discovery."""

import unittest

from pazarama_category_source import (
    CATEGORY_URL, ROBOTS_URL, PazaramaCategorySource,
    _category_page_number, pazarama_product_url,
)


A26 = ('https://www.pazarama.com/samsung-galaxy-a26-5g-128-gb-6-gb-ram-siyah-'
       'cep-telefonu-samsung-turkiye-garantili-p-8806097100638')
S26_FE = 'https://www.pazarama.com/samsung-galaxy-s26-fe-8gb-256gb-p-8806099409753'
CAMON_PRO = 'https://www.pazarama.com/tecno-camon-40-pro-8gb-256gb-p-4894947070952'
A37_WRONG_MEMORY = ('https://www.pazarama.com/samsung-galaxy-a37-akilli-telefon-'
                    'antrasit-256gb-8806099054953-p-8806099054953')
SPECS = {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'}
ROBOTS = ('User-agent: *\nDisallow: /arama\n'
          'Sitemap: https://www.pazarama.com/sitemaps/sitemap.xml\n').encode()


def card(url, title):
    return (f'<a href="{url}" title="{title}">'
            '<div class="product-card__swiper"></div></a>')


def category(*cards, next_url=None):
    link = (f'<a href="{next_url}">Daha Çok Ürün Göster</a>' if next_url else '')
    return ('<html><title>Cep Telefonu Modelleri</title><h1>Cep Telefonu</h1>'
            + ''.join(cards) + link + '</html>').encode()


class Response:
    def __init__(self, url, data=b'', status=200, content_length=None):
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
        self.calls.append((url, kwargs))
        if url not in self.pages:
            raise AssertionError(f'Unexpected request: {url}')
        value = self.pages[url]
        return value if isinstance(value, Response) else Response(url, value)


class PazaramaCategorySourceTests(unittest.TestCase):
    def test_exact_model_ram_storage_and_wrong_suffixes_with_cached_pages(self):
        page2 = 'https://www.pazarama.com/cep-telefonu-k-K03043?ft&isletim-sistemi=android&sayfa=2'
        fetch = Fetch({
            ROBOTS_URL: ROBOTS,
            CATEGORY_URL: category(
                card(A26, 'Samsung Galaxy A26 5G 128 GB 6 GB Ram Siyah Cep Telefonu'),
                card(S26_FE, 'Samsung Galaxy S26 FE 8GB RAM 256GB'),
                card(CAMON_PRO, 'Tecno Camon 40 Pro 8GB RAM 256GB'),
                card(A37_WRONG_MEMORY, 'Samsung Galaxy A37 5G 256 GB 8 GB Ram Antrasit'),
                next_url=page2,
            ),
            page2: category(card(A26, 'Samsung Galaxy A26 5G 128 GB 6 GB Ram Siyah Cep Telefonu')),
        })
        source = PazaramaCategorySource(fetch=fetch, sleep=lambda _: None)
        self.assertEqual(source.discover('Samsung Galaxy A26 5G', SPECS), [{'source_url': A26}])
        self.assertEqual(source.discover('Samsung Galaxy S26', {
            'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g',
        }), [])
        self.assertEqual(source.discover('Tecno Camon 40', {'ram_gb': 8, 'storage_gb': 256}), [])
        self.assertEqual(source.discover('Samsung Galaxy A37 5G', SPECS), [])
        self.assertEqual(source.discover('Samsung Galaxy A26 5G', {'ram_gb': 8, 'storage_gb': 256}), [])
        self.assertEqual([url for url, _ in fetch.calls], [ROBOTS_URL, CATEGORY_URL, page2])
        self.assertEqual(source.report()['category_pages'], 2)

    def test_pdp_fetch_checks_robots_and_caches_same_url(self):
        fetch = Fetch({ROBOTS_URL: ROBOTS, A26: b'<html>product</html>'})
        source = PazaramaCategorySource(fetch=fetch, sleep=lambda _: None)
        self.assertEqual(source.fetch(A26).text, '<html>product</html>')
        self.assertEqual(source.fetch(A26).text, '<html>product</html>')
        self.assertEqual([url for url, _ in fetch.calls], [ROBOTS_URL, A26])
        self.assertEqual(source.report()['pdp_requests'], 1)
        for url in (A26 + '?magaza=other', A26.replace('www.pazarama.com', 'pazarama.com.evil.test'),
                    A26.replace('https:', 'http:'), A26 + '#offer'):
            with self.subTest(url=url), self.assertRaisesRegex(Exception, 'clean'):
                source.fetch(url)

    def test_unavailable_or_disallowing_robots_fails_closed_once(self):
        for rules in (b'<html>blocked</html>', b'User-agent: *\nDisallow: /\n'):
            with self.subTest(rules=rules):
                fetch = Fetch({ROBOTS_URL: rules})
                source = PazaramaCategorySource(fetch=fetch)
                self.assertEqual(source.discover('Samsung Galaxy A26 5G', SPECS), [])
                self.assertEqual(source.discover('Samsung Galaxy A26 5G', SPECS), [])
                self.assertEqual([url for url, _ in fetch.calls], [ROBOTS_URL])

    def test_oversize_category_and_external_pagination_fail_closed(self):
        huge = Response(CATEGORY_URL, b'<html></html>', content_length=5 * 1024 * 1024)
        for value in (huge, category(next_url='https://evil.example/other?sayfa=2')):
            with self.subTest(value=value):
                fetch = Fetch({ROBOTS_URL: ROBOTS, CATEGORY_URL: value})
                source = PazaramaCategorySource(fetch=fetch, sleep=lambda _: None)
                self.assertEqual(source.discover('Samsung Galaxy A26 5G', SPECS), [])
                self.assertEqual(len(fetch.calls), 2)
                if value is huge:
                    self.assertNotEqual(source.report()['status'], 'ready')

    def test_robots_delay_and_denied_category_stop_requests(self):
        delayed = ROBOTS.replace(b'Disallow: /arama', b'Crawl-delay: 2\nDisallow: /arama')
        delays = []
        fetch = Fetch({ROBOTS_URL: delayed, CATEGORY_URL: category()})
        source = PazaramaCategorySource(fetch=fetch, sleep=delays.append, clock=lambda: 0)
        source.discover('Samsung Galaxy A26 5G', SPECS)
        self.assertEqual(delays, [2.0])
        denied = Fetch({ROBOTS_URL: ROBOTS,
                        CATEGORY_URL: Response(CATEGORY_URL, b'blocked', status=403)})
        blocked = PazaramaCategorySource(fetch=denied, sleep=lambda _: None)
        self.assertEqual(blocked.discover('Samsung Galaxy A26 5G', SPECS), [])
        self.assertEqual(blocked.discover('Samsung Galaxy A26 5G', SPECS), [])
        self.assertEqual([url for url, _ in denied.calls], [ROBOTS_URL, CATEGORY_URL])
        self.assertEqual(blocked.report()['http_status'], 403)

    def test_url_boundary_and_bounded_category_pages(self):
        self.assertTrue(pazarama_product_url(A26))
        self.assertFalse(pazarama_product_url('https://www.pazarama.com/arama?q=a26'))
        self.assertFalse(pazarama_product_url(A26.replace('-p-', '%2F-p-')))
        self.assertEqual(_category_page_number(CATEGORY_URL), 1)
        self.assertIsNone(_category_page_number(CATEGORY_URL + '&sayfa=17'))
        self.assertIsNone(_category_page_number(CATEGORY_URL + '&sayfa=2&sayfa=3'))


if __name__ == '__main__':
    unittest.main()
