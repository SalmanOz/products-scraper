"""Offline GitHub-runner audit contract; no live network or ingestion."""

import json
import unittest

from audit_pazarama_source import (BrowserGetTransport, HttpRecorder, PROFILES,
                                   SOLVER_URL, format_summary, run_audit)
from pazarama_category_source import CATEGORY_URL, ROBOTS_URL
from test_pazarama_offer_reader import page, schema


class Response:
    def __init__(self, url, data, status=200):
        self.url, self.data, self.status_code = url, data, status
        self.headers = {'Content-Length': str(len(data))}

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
            raise AssertionError(f'Unexpected audit URL: {url}')
        return Response(url, self.pages[url])


def pages():
    robots = b'User-agent: *\nDisallow: /arama\n'
    cards = ''.join(
        f'<a href="{profile["url"]}" title="{title}">'
        '<div class="product-card__swiper"></div></a>'
        for profile, title in zip(PROFILES, (
            'Samsung Galaxy A26 5G 128 GB 6 GB Ram Siyah Cep Telefonu',
            'Samsung Galaxy A37 5G 6GB/128GB Antrasit Cep Telefonu',
            'Samsung Galaxy S24 FE Gri 128 GB 8 GB Ram Akıllı Telefon',
        ))
    )
    result = {ROBOTS_URL: robots,
              CATEGORY_URL: ('<html><title>Android Cep Telefonu</title><h1>Cep Telefonu</h1>'
                             + cards + '</html>').encode()}
    prices = (
        ('20.000,00 TL', 'Sepette 19.000,00 TL', 19000, 'TURKCELL EMRE İLETİŞİM'),
        ('22.500,00 TL', 'Sepette 21.150,00 TL', 21150, 'BİRTANE TELEKOM'),
        ('38.999,00 TL', None, 38999, 'aradığınız herşey burada'),
    )
    for profile, (normal, discount, schema_price, seller), title in zip(
            PROFILES, prices, (
                'Samsung Galaxy A26 5G 128 GB 6 GB Ram Siyah Cep Telefonu',
                'Samsung Galaxy A37 5G 6GB/128GB Antrasit Cep Telefonu',
                'Samsung Galaxy S24 FE Gri 128 GB 8 GB Ram Akıllı Telefon',
            )):
        product = schema()
        product.update(url=profile['url'], name=title)
        product['offers'].update(price=schema_price, seller=seller)
        result[profile['url']] = page(product, heading=title, regular=normal,
                                      discount=discount, seller=seller).encode()
    return result


class PazaramaSourceAuditTests(unittest.TestCase):
    def test_browser_get_probe_is_bounded_to_reviewed_read_only_urls(self):
        calls = []
        def post(url, **kwargs):
            calls.append((url, kwargs))
            payload = {'status': 'ok', 'solution': {'status': 200,
                       'url': kwargs['json']['url'], 'response': 'User-agent: *\nAllow: /\n'}}
            return Response(SOLVER_URL, json.dumps(payload).encode())
        browser = BrowserGetTransport(post=post)
        response = browser(ROBOTS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.iter_content(10)), b'User-agent: *\nAllow: /\n')
        self.assertEqual(calls[0][1]['json']['cmd'], 'request.get')
        self.assertEqual(browser.diagnostics[0]['reason'], 'site_response')
        self.assertEqual(browser.diagnostics[0]['solution_status'], 200)
        with self.assertRaisesRegex(Exception, 'Unreviewed'):
            browser('https://www.pazarama.com/arama?q=galaxy')
        self.assertEqual(len(calls), 1)

    def test_browser_get_failure_records_stage_without_response_body(self):
        def post(_url, **_kwargs):
            return Response(SOLVER_URL, b'{"status":"error","message":"private challenge text"}')
        browser = BrowserGetTransport(post=post)
        with self.assertRaisesRegex(Exception, 'Invalid browser GET result'):
            browser(ROBOTS_URL)
        self.assertEqual(browser.diagnostics[0]['reason'], 'solver_reported_failure')
        self.assertNotIn('message', browser.diagnostics[0])

    def test_browser_get_http_500_records_sanitized_service_message(self):
        def post(_url, **_kwargs):
            data = b'{"message":"TimeoutException while loading https://www.pazarama.com/robots.txt"}'
            return Response(SOLVER_URL, data, status=500)
        browser = BrowserGetTransport(post=post)
        with self.assertRaisesRegex(Exception, 'unavailable'):
            browser(ROBOTS_URL)
        diagnostic = browser.diagnostics[0]
        self.assertEqual(diagnostic['solver_http_status'], 500)
        self.assertIn('TimeoutException', diagnostic['solver_error_excerpt'])
        self.assertNotIn('https://', diagnostic['solver_error_excerpt'])

    def test_three_exact_variants_and_http_statuses_are_reported_without_writes(self):
        fetch = Fetch(pages())
        report = run_audit(recorder=HttpRecorder(fetch=fetch))
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['verified_products'], 3)
        self.assertEqual(report['category']['category_pages'], 1)
        self.assertEqual([event['http_status'] for event in report['http_events']], [200] * 5)
        self.assertEqual([row['verified_offers'][0]['price'] for row in report['products']],
                         [20000, 22500, 38999])
        self.assertEqual([row['price_evidence']['schema_offer_price'] for row in report['products']],
                         [19000, 21150, 38999])
        self.assertIn('Robots HTTP 200', format_summary(report))
        self.assertTrue(all(kwargs['allow_redirects'] is False for _, kwargs in fetch.calls))

    def test_discovery_gap_does_not_pass_on_reviewed_pdp_alone(self):
        fixture = pages()
        fixture[CATEGORY_URL] = b'<html><title>Android</title><h1>Cep Telefonu</h1></html>'
        report = run_audit(recorder=HttpRecorder(fetch=Fetch(fixture)))
        self.assertEqual(report['verified_products'], 0)
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(all(row['status'] == 'pdp_verified_but_not_discovered'
                            for row in report['products']))


if __name__ == '__main__':
    unittest.main()
