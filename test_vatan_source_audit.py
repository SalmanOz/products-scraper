"""Offline runner-audit checks; every network request is an injected fixture."""

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

from audit_vatan_source import PROFILES, format_summary, main, probe_browser_sitemap, run_audit, write_outputs
from retailer_offer_refresh import RetailerOfferRefresher
from vatan_sitemap_source import ROBOTS_URL, SITEMAP_URL, VatanSitemapSource


URLS = (
    'https://www.vatanbilgisayar.com/iphone-17-256-gb-akilli-telefon-beyaz.html',
    'https://www.vatanbilgisayar.com/xiaomi-17t-12-256gb-akilli-telefon-koyu-mor.html',
    'https://www.vatanbilgisayar.com/samsung-galaxy-s26-12-256-gb-akilli-telefon-kobalt-mor.html',
)
TITLES = (
    'iPhone 17 256 GB Akıllı Telefon Beyaz',
    'Xiaomi 17T 12/256GB Akıllı Telefon Koyu Mor',
    'Samsung Galaxy S26 5G 12/256 GB Akıllı Telefon Kobalt Mor',
)
PRICES = (89999, 54999, 68999)


class Response:
    def __init__(self, url, body, status=200):
        self.url, self.status_code = url, status
        self.data = body if isinstance(body, bytes) else body.encode()
        self.text = self.data.decode()
        self.headers = {'Content-Length': str(len(self.data))}

    def iter_content(self, chunk_size):
        for index in range(0, len(self.data), chunk_size):
            yield self.data[index:index + chunk_size]

    def close(self):
        pass


class Fetch:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, **_kwargs):
        self.calls.append(url)
        if url not in self.pages:
            raise AssertionError(f'Unexpected request: {url}')
        page = self.pages[url]
        return page if isinstance(page, Response) else Response(url, page)


def product_page(url, title, price, sku):
    schema = {'@type': 'Product', 'name': title, 'sku': sku,
              'hasVariant': [{'@type': 'Product', 'sku': str(int(sku) + 1),
                              'url': url.replace('.html', '-other.html')}],
              'offers': {'@type': 'Offer', 'url': url, 'price': str(price),
                         'priceCurrency': 'TRY', 'availability': 'https://schema.org/InStock',
                         'itemCondition': 'https://schema.org/NewCondition',
                         'seller': {'@type': 'Organization', 'name': 'Vatan Bilgisayar',
                                    'url': 'https://www.vatanbilgisayar.com'}}}
    displayed = f'{price:,}'.replace(',', '.') + ' TL'
    return ('<div class="product-detail"><h1 class="product-list__product-name">' + title
            + '</h1><div class="product-id">MODEL / ' + sku + '</div>'
            + '<div class="product-detail-price">' + displayed + '</div>'
            + '<button id="add-to-cart-button" class="detail-cart-button basketBTN">Sepete Ekle</button>'
            + '</div><script type="application/ld+json">' + json.dumps(schema, ensure_ascii=False)
            + '</script>')


def components(*, xiaomi_status=200, robots_status=200):
    sitemap = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               + ''.join(f'<url><loc>{escape(url)}</loc></url>' for url in URLS) + '</urlset>')
    pages = {
        ROBOTS_URL: Response(ROBOTS_URL, f'User-agent: *\nDisallow: /arama/*\nSitemap: {SITEMAP_URL}\n', robots_status),
        SITEMAP_URL: sitemap,
    }
    for url, title, price, sku in zip(URLS, TITLES, PRICES, ('153480', '157948', '156532')):
        pages[url] = Response(url, product_page(url, title, price, sku),
                              xiaomi_status if url == URLS[1] else 200)
    fetch = Fetch(pages)
    return VatanSitemapSource(fetch=fetch, sleep=lambda _: None), RetailerOfferRefresher(fetch=fetch), fetch


class VatanRunnerAuditTests(unittest.TestCase):
    def test_browser_sitemap_probe_requires_exact_200_xml(self):
        xml = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               f'<url><loc>{URLS[0]}</loc></url></urlset>')

        def post(solution):
            return lambda url, **kwargs: Response(url, json.dumps({
                'status': 'ok', 'solution': solution,
            }))

        valid = probe_browser_sitemap(post=post({
            'status': 200, 'url': SITEMAP_URL, 'response': xml,
        }))
        self.assertEqual(valid, {'status': 'valid_urlset', 'http_status': 200, 'url_count': 1})
        blocked = probe_browser_sitemap(post=post({
            'status': 403, 'url': SITEMAP_URL, 'response': 'blocked',
        }))
        self.assertEqual(blocked['status'], 'site_http_403')
        challenge = probe_browser_sitemap(post=post({
            'status': 200, 'url': SITEMAP_URL, 'response': '<html>challenge</html>',
        }))
        self.assertEqual(challenge['status'], 'not_urlset')

    def test_three_exact_live_pdp_observations_are_reported_without_writes(self):
        source, reader, fetch = components()
        report = run_audit(source, reader)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['verified_products'], 3)
        self.assertEqual([row['slug'] for row in report['products']], [p['slug'] for p in PROFILES])
        self.assertEqual([row['verified_offers'][0]['price'] for row in report['products']], list(PRICES))
        self.assertTrue(all(row['source_attempts'][0]['reason'] == 'observed_exact_offer'
                            for row in report['products']))
        self.assertEqual(fetch.calls, [ROBOTS_URL, SITEMAP_URL, *URLS])
        self.assertIn('3/3 exact catalog profiles verified', format_summary(report))

    def test_access_denial_yields_partial_artifact_with_host_circuit_reasons(self):
        source, reader, fetch = components(xiaomi_status=403)
        report = run_audit(source, reader)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['verified_products'], 1)
        self.assertEqual(report['products'][1]['source_attempts'][0]['reason'], 'access_denied')
        self.assertEqual(report['products'][2]['source_attempts'][0]['reason'], 'host_circuit_open')
        self.assertNotIn(URLS[2], fetch.calls)
        with TemporaryDirectory() as directory:
            json_path = Path(directory) / 'audit.json'
            markdown_path = Path(directory) / 'audit.md'
            write_outputs(report, json_path, markdown_path)
            self.assertEqual(json.loads(json_path.read_text())['verified_products'], 1)
            self.assertIn('access_denied', markdown_path.read_text())

    def test_unavailable_robots_never_requests_sitemap_or_pdp(self):
        source, reader, fetch = components(robots_status=403)
        report = run_audit(source, reader)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['verified_products'], 0)
        self.assertEqual(fetch.calls, [ROBOTS_URL])
        self.assertTrue(all(row['status'] == 'no_sitemap_candidate' for row in report['products']))

    def test_command_fails_after_writing_partial_result(self):
        source, reader, _ = components(xiaomi_status=403)
        with TemporaryDirectory() as directory, patch('audit_vatan_source.run_audit',
                                                     return_value=run_audit(source, reader)), \
                patch('audit_vatan_source.REPORT_PATH', Path(directory) / 'audit.json'), \
                patch('audit_vatan_source.SUMMARY_PATH', Path(directory) / 'audit.md'):
            with redirect_stdout(StringIO()):
                self.assertEqual(main(), 1)
            self.assertTrue((Path(directory) / 'audit.json').is_file())
            self.assertTrue((Path(directory) / 'audit.md').is_file())


if __name__ == '__main__':
    unittest.main()
