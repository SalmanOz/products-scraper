import json
import unittest
from unittest.mock import patch

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
    def test_one_transient_failure_cools_down_then_allows_one_later_healthy_pdp(self):
        now, calls = [1000], []
        def get(url, **_):
            calls.append(url)
            if url.endswith('robots.txt'):
                return Response(url, 'User-agent: *\nAllow: /\n')
            return Response(url, '<h1>Phone</h1>', status=503 if url.endswith(('1234', '1237')) else 200)
        source = PriorityRetailerTransport(get=get, sleep=lambda _: None, clock=lambda: now[0], maximum_per_host=5)
        self.assertEqual(source.read_document(URLS[0])[1], 'http_503')
        second = URLS[0].replace('1234', '1235')
        self.assertEqual(source.read_document(second)[1], 'source_cooldown')
        self.assertEqual(len(calls), 2)
        now[0] += 31
        self.assertEqual(source.read_document(second)[1], 'document_read')
        self.assertEqual(source.read_document(URLS[0].replace('1234', '1237'))[1], 'http_503')
        self.assertEqual(source.read_document(URLS[0].replace('1234', '1238'))[1], 'source_circuit_open')

    def test_explicit_browser_denial_is_never_misclassified_as_a_retryable_service_500(self):
        def get(url, **_):
            return Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else 'denied',
                            status=200 if url.endswith('robots.txt') else 403)
        for message, reason, status in (
            ('Error: Cloudflare has blocked this request. Probably your IP is banned.', 'browser_access_denied', 403),
            ('Error: Timeout after 45.0 seconds.', 'browser_timeout', 504),
        ):
            source = PriorityRetailerTransport(get=get, sleep=lambda _: None,
                post=lambda url, **_: Response(url, json.dumps({'status': 'error', 'message': message}), status=500))
            self.assertEqual(source.read_document(URLS[2])[1:], (reason, status))
            self.assertEqual(source.read_document(URLS[2] + '1')[1],
                             'source_circuit_open' if status == 403 else 'source_cooldown')

    def test_n11_discovery_search_obeys_robots_and_shared_document_budget(self):
        calls = []
        def get(url, **_):
            calls.append(url)
            return Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Search</h1>')
        source = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=2)
        self.assertEqual(source.read_n11_search('Samsung Galaxy A57 256 GB')[1], 'document_read')
        self.assertEqual(source.read_document(URLS[2])[1], 'document_read')
        self.assertEqual(source.read_n11_search('Samsung Galaxy A56 256 GB')[1], 'pdp_budget_exhausted')
        self.assertEqual(source.report()['n11']['search_requests'], 1)
        self.assertEqual(source.report()['n11']['pdp_requests'], 1)
        self.assertEqual(source.report()['n11']['document_requests'], 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual(source.read_n11_search('bad\nquery')[1], 'invalid_catalog_query')
        denied = PriorityRetailerTransport(get=lambda url, **_: Response(url,
            'User-agent: *\nDisallow: /arama\n'), sleep=lambda _: None)
        self.assertEqual(denied.read_n11_search('Samsung Galaxy A57 256 GB')[1], 'robots_disallowed')

    def test_compatible_http_retains_bot_identity_robots_streaming_and_no_redirects(self):
        calls = []
        def get(url, **kwargs):
            calls.append((url, kwargs))
            return Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Phone</h1>')
        with patch('priority_retailer_transport.compatible_requests.get', side_effect=get):
            source = PriorityRetailerTransport(sleep=lambda _: None)
            self.assertEqual(source.read_document(URLS[2])[1], 'document_read')
        self.assertEqual(len(calls), 2)
        for _, options in calls:
            self.assertEqual(options['impersonate'], 'chrome')
            self.assertIn('TeknoskorPriceBot', options['headers']['User-Agent'])
            self.assertFalse(options['allow_redirects'])
            self.assertTrue(options['stream'])
            self.assertEqual(options['timeout'], (5, 15))

    def test_refresh_budget_reserves_the_four_requested_retailers_first(self):
        from offer_target_selection import select_offer_targets
        other = [{'source_url': 'https://www.idefix.com/phone-p-123'},
                 {'source_url': 'https://www.mediamarkt.com.tr/tr/product/_phone-123456.html'}]
        selected = select_offer_targets(other + [{'source_url': url} for url in reversed(URLS)], maximum=4)
        self.assertEqual([row['merchant_name'] for row in selected],
                         ['Hepsiburada', 'Trendyol', 'n11', 'Teknosa'])

    def test_only_exact_priority_pdp_shapes_and_bounded_seller_queries(self):
        for url in URLS:
            self.assertIsNotNone(product_host(url))
            for bad in (url.replace('https:', 'http:'), url + '?token=private',
                        url.replace('www.', 'user:pass@www.'), url + '#fragment',
                        url.replace('.com/', '.com.evil.test/'), url + '%00'):
                self.assertIsNone(product_host(bad))
        self.assertIsNone(product_host('https://www.trendyol.com/sr?q=Samsung'))
        self.assertIsNotNone(product_host(URLS[1] + '?merchantId=1234&boutiqueId=61'))
        self.assertIsNotNone(product_host(URLS[1] + '?merchantId=1234&v=tek-ebat'))
        self.assertIsNone(product_host(URLS[1] + '?merchantId=1234&v=512-gb'))
        self.assertIsNotNone(product_host(URLS[0] + '?magaza=OnePlus+T%C3%BCrkiye'))
        self.assertIsNone(product_host(URLS[0] + '?magaza=%00'))
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

    def test_readonly_broad_sample_is_bounded_and_covers_both_ends_without_manual_ids(self):
        products = [{'id': i, 'slug': str(i), 'offer_targets': [{'source_url': URLS[1] + str(i)}]}
                    for i in range(1, 21)]
        selected = queue(products, {}, maximum=8, spread=True)['www.trendyol.com']
        self.assertEqual(len(selected), 8)
        self.assertEqual((selected[0][1]['id'], selected[-1][1]['id']), (20, 1))
        self.assertEqual(len({p['id'] for _, p in selected}), 8)
        with self.assertRaises(ValueError):
            queue(products, {}, maximum=60)

    def test_full_catalog_budget_does_not_stop_after_sixty_of_110_products(self):
        source = PriorityRetailerTransport(sleep=lambda _: None, get=lambda url, **_:
            Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Phone</h1>'))
        source.configure_catalog_budget(110)
        for number in range(110):
            self.assertEqual(source.read_document(URLS[1] + str(number))[1], 'document_read')
        self.assertEqual(source.report()['Trendyol']['pdp_requests'], 110)
        self.assertEqual(source.read_document(URLS[1] + 'extra')[1], 'invalid_priority_pdp')
        self.assertEqual(source.read_document(URLS[1] + '110')[1], 'pdp_budget_exhausted')
        with self.assertRaises(RuntimeError):
            source.configure_catalog_budget(200)

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

    def test_category_discovery_does_not_spend_canonical_pdp_redirect_reserve(self):
        from trendyol_catalog_discovery import CATEGORY_URL
        original = URLS[1] + '?v=tek-ebat'
        canonical = URLS[1].replace('128-gb', '128gb')
        def get(url, **_):
            response = Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Phone</h1>')
            if url == original:
                response.status_code = 301
                response.headers['Location'] = canonical
            return response
        reader = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=2)
        self.assertEqual(reader.read_trendyol_category(CATEGORY_URL)[1], 'document_read')
        self.assertEqual(reader.read_document(original)[1], 'document_read')
        self.assertEqual(reader.report()['Trendyol']['pdp_requests'], 2)
        self.assertEqual(reader.read_document(URLS[1] + '1')[1], 'pdp_budget_exhausted')

    def test_n11_canonical_redirect_keeps_group_robots_budget_and_one_hop(self):
        original = URLS[2]
        canonical = original.replace('128-gb', '128gb')
        for destination, expected in (
            (canonical, 'document_read'),
            (canonical + '?magaza=other', 'redirect_identity_or_policy_mismatch'),
            (canonical.replace('12345678', '12345679'), 'redirect_identity_or_policy_mismatch'),
            ('https://evil.example/urun/phone-12345678', 'redirect_identity_or_policy_mismatch'),
        ):
            calls = []
            def get(url, **_):
                calls.append(url)
                response = Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Phone</h1>')
                if url == original:
                    response.status_code = 301
                    response.headers['Location'] = destination
                return response
            reader = PriorityRetailerTransport(get=get, sleep=lambda _: None)
            self.assertEqual(reader.read_document(original)[1], expected)
            self.assertEqual(len(calls), 3 if expected == 'document_read' else 2)
            if expected == 'document_read':
                self.assertEqual(reader.resolved_url(original), canonical)
        reader = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=1)
        destination = canonical
        self.assertEqual(reader.read_document(original)[1], 'redirect_budget_or_duplicate')

        def denied(url, **_):
            response = get(url)
            if url.endswith('robots.txt'):
                # Only the redirected spelling is forbidden. Avoid overlapping
                # Allow/Disallow precedence, which differs across Python versions.
                response = Response(url, 'User-agent: *\nDisallow: ' + canonical.split('.com')[1])
            return response
        reader = PriorityRetailerTransport(get=denied, sleep=lambda _: None)
        self.assertEqual(reader.read_document(original)[1], 'redirect_identity_or_policy_mismatch')

        def looping(url, **_):
            if url.endswith('robots.txt'):
                return Response(url, 'User-agent: *\nAllow: /\n')
            response = Response(url, '', status=301)
            response.headers['Location'] = canonical if url == original else original
            return response
        reader = PriorityRetailerTransport(get=looping, sleep=lambda _: None)
        self.assertEqual(reader.read_document(original)[1], 'http_301')
        self.assertEqual(reader.report()['n11']['pdp_requests'], 2)

    def test_trendyol_canonical_redirect_keeps_product_seller_robots_and_request_budget(self):
        original = URLS[1] + '?merchantId=123&v=tek-ebat'
        canonical = URLS[1].replace('128-gb', '128gb') + '?merchantId=123'
        for destination, expected in (
            (canonical, 'document_read'),
            (canonical.replace('merchantId=123', 'merchantId=999'), 'redirect_identity_or_policy_mismatch'),
            (canonical.split('?')[0], 'redirect_identity_or_policy_mismatch'),
            (canonical.replace('676507062', '676507063'), 'redirect_identity_or_policy_mismatch'),
            ('https://attacker.example/phone', 'redirect_identity_or_policy_mismatch'),
        ):
            calls = []
            def get(url, **kwargs):
                calls.append(url)
                self.assertFalse(kwargs['allow_redirects'])
                response = Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Phone</h1>')
                if url == original:
                    response.status_code = 301
                    response.headers['Location'] = destination
                return response
            reader = PriorityRetailerTransport(get=get, sleep=lambda _: None)
            with self.subTest(destination=destination):
                self.assertEqual(reader.read_document(original)[1], expected)
                self.assertEqual(len(calls), 3 if expected == 'document_read' else 2)
                if expected == 'document_read':
                    self.assertEqual(reader.resolved_url(original), canonical)
        destination = canonical
        reader = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=1)
        self.assertEqual(reader.read_document(original)[1], 'redirect_budget_or_duplicate')

        def looping_get(url, **_):
            if url.endswith('robots.txt'):
                return Response(url, 'User-agent: *\nAllow: /\n')
            result = Response(url, '', status=301)
            result.headers['Location'] = canonical if url == original else original
            return result
        reader = PriorityRetailerTransport(get=looping_get, sleep=lambda _: None, maximum_per_host=8)
        self.assertEqual(reader.read_document(original)[1], 'http_301')
        self.assertEqual(reader.report()['Trendyol']['pdp_requests'], 2)


if __name__ == '__main__':
    unittest.main()
