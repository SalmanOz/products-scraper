import json
import unittest

from audit_priority_retailers import queue
from priority_retailer_transport import PriorityRetailerTransport, product_host
from test_pasaj_source import Response


URLS = (
    'https://www.hepsiburada.com/samsung-galaxy-a57-128-gb-p-HBCV0000AB1234',
    'https://www.trendyol.com/samsung/galaxy-a54-128-gb-p-676507062',
    'https://www.n11.com/urun/samsung-galaxy-a54-128-gb-12345678',
    'https://www.teknosa.com/samsung-galaxy-a54-128-gb-p-100000123456',
)


class PriorityRetailerTests(unittest.TestCase):
    def test_only_exact_priority_pdp_shapes_and_bounded_seller_queries(self):
        for url in URLS:
            self.assertIsNotNone(product_host(url))
            for bad in (url.replace('https:', 'http:'), url + '?token=private',
                        url.replace('www.', 'user:pass@www.'), url + '#fragment',
                        url.replace('.com/', '.com.evil.test/'), url + '%00'):
                self.assertIsNone(product_host(bad))
        self.assertIsNone(product_host('https://www.trendyol.com/sr?q=Samsung'))
        self.assertIsNotNone(product_host(URLS[1] + '?merchantId=1234&boutiqueId=61'))
        self.assertIsNone(product_host(URLS[1] + '?merchantId=1&merchantId=2'))

    def test_catalog_queue_prefers_unpriced_deduplicates_and_never_exceeds_two_per_host(self):
        products = [{'id': i, 'slug': str(i), 'offer_targets': [{'source_url': URLS[1] + str(i)}]}
                    for i in range(1, 6)]
        prices = {i: (str(i), i != 2) for i in range(1, 6)}
        selected = queue(products, prices)['www.trendyol.com']
        self.assertEqual([p['id'] for _, p in selected], [2, 5])
        products.append({'id': 6, 'slug': '6', 'offer_targets': products[1]['offer_targets']})
        self.assertNotIn(URLS[1] + '2', [url for url, _ in queue(products, prices)['www.trendyol.com']])

    def test_denied_direct_get_uses_one_exact_browser_document_and_paces(self):
        url = URLS[1]
        calls, posts, sleeps = [], [], []
        def get(target, **kwargs):
            calls.append(target)
            self.assertFalse(kwargs['allow_redirects'])
            return Response(target, 'User-agent: *\nAllow: /\n' if target.endswith('robots.txt') else 'denied',
                            status=200 if target.endswith('robots.txt') else 403)
        def post(target, **kwargs):
            posts.append(kwargs['json']['url'])
            return Response(target, json.dumps({'status': 'ok', 'solution':
                {'url': url, 'status': 200, 'response': '<html><h1>Phone</h1></html>'}}))
        transport = PriorityRetailerTransport(get=get, post=post, sleep=sleeps.append, clock=lambda: 0)
        self.assertEqual(transport.read_document(url)[1:], ('document_read', 200))
        self.assertEqual(posts, [url])
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(transport.read_document(url)[1], 'already_read')
        self.assertEqual(len(calls), 2)

    def test_robots_denial_challenge_and_budget_prevent_further_reads(self):
        for policy, body, expected in (
            ('User-agent: *\nDisallow: /\n', '<h1>Phone</h1>', 'robots_disallowed'),
            ('User-agent: *\nAllow: /\n', '<title>Hepsiburada | Güvenlik</title>', 'challenge_page'),
        ):
            calls = []
            def get(url, **_):
                calls.append(url)
                return Response(url, policy if url.endswith('robots.txt') else body)
            source = PriorityRetailerTransport(get=get, sleep=lambda _: None)
            self.assertEqual(source.read_document(URLS[0])[1], expected)
            if expected == 'challenge_page':
                self.assertEqual(source.read_document(URLS[0].replace('AB1234', 'AB1235'))[1], 'source_circuit_open')
                self.assertEqual(len(calls), 2)
        source = PriorityRetailerTransport(maximum_per_host=1, sleep=lambda _: None,
            get=lambda url, **_: Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Phone</h1>'))
        source.read_document(URLS[1])
        self.assertEqual(source.read_document(URLS[1] + '1')[1], 'pdp_budget_exhausted')


if __name__ == '__main__':
    unittest.main()
